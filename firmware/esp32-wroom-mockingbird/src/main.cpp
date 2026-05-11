// mockingbird ESP32-WROOM-32 leaf firmware (BLE scanner).
//
//   WiFi STA (Mockingbird)
//     │
//     ├── ArduinoOTA listener on UDP 3232
//     │
//     ├── HTTP server on :80
//     │     GET  /            status JSON
//     │     GET  /version     plain text version
//     │     POST /restart     reboot
//     │     POST /scan/reset  clear the observation table, restart window
//     │     GET  /scan/result JSON dump of every BLE advertiser seen since
//     │                       /scan/reset (or since boot), with aggregated
//     │                       RSSI min/max/avg + advertised name + mfg data
//     │
//     └── NimBLE scanner running continuously in the background, accumulating
//         observations into an in-memory map. Adv callback fires on every
//         packet — `wantDup=true` so we get per-packet RSSI samples, not just
//         first-seen.
//
// The HTTP /scan/* endpoints are how the Pi aggregator pulls per-leaf data.
// Two leaves doing simultaneous 60s captures from different physical spots
// is the smallest meaningful "distributed BLE sensing" experiment.

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <NimBLEDevice.h>
#include <esp_system.h>

#include <map>
#include <mutex>
#include <string>

#include "secrets.h"

static const char *FW_VERSION = MOCKINGBIRD_FW_VERSION;

static String deviceHostname() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char buf[24];
    snprintf(buf, sizeof(buf), "mockingbird-%02x%02x%02x", mac[3], mac[4], mac[5]);
    return String(buf);
}

static WebServer server(80);

// ---------- BLE observation aggregator ----------

struct Obs {
    int      rssi_min = 127;
    int      rssi_max = -127;
    int32_t  rssi_sum = 0;
    uint32_t count    = 0;
    uint32_t first_ms = 0;
    uint32_t last_ms  = 0;
    std::string name;     // sanitized to printable ASCII
    std::string manuf;    // hex of first 16 bytes of mfg data, lowercase
    uint8_t  addr_type = 0;
};

static std::map<std::string, Obs> g_obs;
static std::mutex                 g_obs_mu;
static uint32_t                   g_scan_start_ms = 0;

class ScanCB : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice *d) override {
        std::string mac  = d->getAddress().toString();
        int         rssi = d->getRSSI();
        uint8_t     at   = d->getAddressType();

        // Sanitize name to printable, JSON-safe ASCII.
        std::string name;
        if (d->haveName()) {
            const std::string &raw = d->getName();
            for (char c : raw) {
                if (c >= 0x20 && c < 0x7f && c != '"' && c != '\\') name += c;
            }
        }

        // Manufacturer data: hex-encode first 16 bytes.
        std::string manuf_hex;
        if (d->haveManufacturerData()) {
            std::string m = d->getManufacturerData();
            char buf[33] = {0};
            size_t n = m.size() < 16 ? m.size() : 16;
            for (size_t i = 0; i < n; i++) {
                snprintf(&buf[i * 2], 3, "%02x", (unsigned char)m[i]);
            }
            manuf_hex = buf;
        }

        std::lock_guard<std::mutex> g(g_obs_mu);
        auto &e = g_obs[mac];
        if (e.count == 0) {
            e.first_ms  = millis();
            e.name      = name;
            e.manuf     = manuf_hex;
            e.addr_type = at;
        }
        e.last_ms = millis();
        e.count++;
        e.rssi_sum += rssi;
        if (rssi < e.rssi_min) e.rssi_min = rssi;
        if (rssi > e.rssi_max) e.rssi_max = rssi;
    }
};

// ---------- HTTP handlers ----------

static void handleStatus() {
    char body[400];
    snprintf(body, sizeof(body),
             "{\"hostname\":\"%s\",\"version\":\"%s\","
             "\"uptime_s\":%lu,\"rssi\":%d,\"free_heap\":%u,"
             "\"ip\":\"%s\",\"mac\":\"%s\","
             "\"ble\":{\"n_unique\":%u,\"scan_window_ms\":%lu}}",
             deviceHostname().c_str(), FW_VERSION,
             (unsigned long)(millis() / 1000UL),
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(),
             WiFi.localIP().toString().c_str(),
             WiFi.macAddress().c_str(),
             (unsigned)g_obs.size(),
             (unsigned long)(millis() - g_scan_start_ms));
    server.send(200, "application/json", body);
}

static void handleVersion() { server.send(200, "text/plain", FW_VERSION); }

static void handleRestart() {
    server.send(200, "application/json", "{\"status\":\"restarting\"}");
    delay(200);
    ESP.restart();
}

static void handleScanReset() {
    std::lock_guard<std::mutex> g(g_obs_mu);
    g_obs.clear();
    g_scan_start_ms = millis();
    char b[96];
    snprintf(b, sizeof(b), "{\"status\":\"reset\",\"at_ms\":%lu}",
             (unsigned long)g_scan_start_ms);
    server.send(200, "application/json", b);
}

// Streamed JSON: a couple hundred entries × ~150 bytes each can exceed
// 30 KB. Build it once in RAM is fine on the heap (~250 KB free typical),
// but use chunked transfer to avoid the WebServer's Content-Length pre-
// computation problem.
static void handleScanResult() {
    std::lock_guard<std::mutex> g(g_obs_mu);

    uint32_t total = 0;
    for (auto &kv : g_obs) total += kv.second.count;

    server.setContentLength(CONTENT_LENGTH_UNKNOWN);
    server.send(200, "application/json", "");

    char hdr[280];
    snprintf(hdr, sizeof(hdr),
             "{\"hostname\":\"%s\",\"version\":\"%s\","
             "\"now_ms\":%lu,\"scan_start_ms\":%lu,\"scan_window_ms\":%lu,"
             "\"n_unique\":%u,\"n_total_obs\":%lu,"
             "\"observations\":[",
             deviceHostname().c_str(), FW_VERSION,
             (unsigned long)millis(), (unsigned long)g_scan_start_ms,
             (unsigned long)(millis() - g_scan_start_ms),
             (unsigned)g_obs.size(), (unsigned long)total);
    server.sendContent(hdr);

    bool first = true;
    char rec[512];
    for (auto &kv : g_obs) {
        const auto &e = kv.second;
        snprintf(rec, sizeof(rec),
                 "%s{\"mac\":\"%s\",\"addr_type\":%u,\"count\":%lu,"
                 "\"rssi_min\":%d,\"rssi_max\":%d,\"rssi_avg\":%d,"
                 "\"first_ms\":%lu,\"last_ms\":%lu,"
                 "\"name\":\"%s\",\"manuf\":\"%s\"}",
                 first ? "" : ",",
                 kv.first.c_str(), (unsigned)e.addr_type,
                 (unsigned long)e.count,
                 e.rssi_min, e.rssi_max,
                 (int)(e.rssi_sum / (int32_t)e.count),
                 (unsigned long)e.first_ms, (unsigned long)e.last_ms,
                 e.name.c_str(), e.manuf.c_str());
        server.sendContent(rec);
        first = false;
    }
    server.sendContent("]}");
}

// ---------- WiFi + OTA ----------

static void connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.setHostname(deviceHostname().c_str());
    WiFi.begin(MOCKINGBIRD_WIFI_SSID, MOCKINGBIRD_WIFI_PSK);
    Serial.printf("[wifi] connecting to %s ...", MOCKINGBIRD_WIFI_SSID);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print('.');
    }
    Serial.printf("\n[wifi] %s  rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

static void startOTA() {
    ArduinoOTA.setHostname(deviceHostname().c_str());
    ArduinoOTA.onStart([] {
        Serial.printf("[ota] start: %s\n",
                      ArduinoOTA.getCommand() == U_FLASH ? "flash" : "spiffs");
        // Stop BLE during OTA to free flash-write bandwidth and avoid
        // weird BLE-stack-during-firmware-write interactions.
        if (NimBLEDevice::getScan()->isScanning()) {
            NimBLEDevice::getScan()->stop();
        }
    });
    ArduinoOTA.onEnd([] { Serial.println("\n[ota] end"); });
    ArduinoOTA.onProgress([](unsigned int p, unsigned int t) {
        Serial.printf("[ota] %u%%\r", (p * 100) / t);
    });
    ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[ota] error %u\n", e); });
    ArduinoOTA.begin();
    Serial.printf("[ota] listening as %s.local (port 3232)\n",
                  deviceHostname().c_str());
}

// ---------- BLE scan startup ----------

static void startBLE() {
    NimBLEDevice::init("");
    auto *pScan = NimBLEDevice::getScan();
    pScan->setAdvertisedDeviceCallbacks(new ScanCB(), /*wantDup=*/true);
    pScan->setActiveScan(true);   // request scan-response data (more name/mfg info)
    pScan->setInterval(100);      // ms between scan windows
    pScan->setWindow(99);         // dwell within each window
    pScan->setMaxResults(0);      // don't accumulate in NimBLE's own buffer
    g_scan_start_ms = millis();
    pScan->start(0, nullptr, false);  // forever, no completion CB
    Serial.println("[ble] scanner running (continuous, active)");
}

// ---------- main ----------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n=== mockingbird leaf %s booted, version %s ===\n",
                  deviceHostname().c_str(), FW_VERSION);

    connectWiFi();

    if (!MDNS.begin(deviceHostname().c_str())) {
        Serial.println("[mdns] FAILED to start");
    } else {
        MDNS.addService("http", "tcp", 80);
        MDNS.addService("mockingbird", "tcp", 80);
    }

    startOTA();
    startBLE();

    server.on("/",            HTTP_GET,  handleStatus);
    server.on("/version",     HTTP_GET,  handleVersion);
    server.on("/restart",     HTTP_POST, handleRestart);
    server.on("/scan/reset",  HTTP_POST, handleScanReset);
    server.on("/scan/result", HTTP_GET,  handleScanResult);
    server.begin();
    Serial.println("[http] listening on :80");
}

void loop() {
    ArduinoOTA.handle();
    server.handleClient();
    delay(1);
}
