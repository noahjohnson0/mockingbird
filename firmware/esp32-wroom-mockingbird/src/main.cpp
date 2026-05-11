// mockingbird ESP32-WROOM-32 leaf firmware (BLE → TCP streamer, v0.3.0-stream).
//
// Architecture is now PUSH instead of PULL:
//   • BLE callback enqueues each observation into a small FreeRTOS queue
//     (64 slots, ~6 KB total) and returns ASAP — zero accumulation on-device.
//   • A dedicated uplink task drains the queue and writes each observation
//     as a newline-delimited JSON record to mockingbird-pi:9001 over TCP.
//   • The Pi runs `services/mockingbird-collector.py` which persists every
//     record to an indexed SQLite database for time-series analysis.
//
// This kills the OOM-reboot bug from v0.2.x: the leaves no longer hold the
// 256-entry observation table that was pressuring the heap during dense
// scans. They also no longer crash if the BLE neighborhood grows large.
//
// HTTP endpoints retained (minimal):
//     GET  /            status JSON (now includes uplink counters)
//     GET  /version
//     POST /restart
//
// The /scan/reset and /scan/result endpoints from v0.2.x are gone — the Pi
// has every observation continuously, so scan windows are an artifact of
// the database query, not the device.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <NimBLEDevice.h>
#include <esp_system.h>

#include "secrets.h"

static const char *FW_VERSION = MOCKINGBIRD_FW_VERSION;

#ifndef MOCKINGBIRD_COLLECTOR_HOST
#define MOCKINGBIRD_COLLECTOR_HOST "192.168.8.202"
#endif
#ifndef MOCKINGBIRD_COLLECTOR_PORT
#define MOCKINGBIRD_COLLECTOR_PORT 9001
#endif

static String deviceHostname() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char buf[24];
    snprintf(buf, sizeof(buf), "mockingbird-%02x%02x%02x", mac[3], mac[4], mac[5]);
    return String(buf);
}

static WebServer server(80);

// ---------- streaming message queue ----------

struct Msg {
    char     mac[18];     // "AA:BB:CC:DD:EE:FF\0"
    int8_t   rssi;
    uint8_t  addr_type;
    char     name[24];    // truncated/sanitized
    char     manuf[33];   // hex of first 16 mfg-data bytes + NUL
    uint32_t t_ms;
};

#define MSG_QUEUE_LEN 64

static QueueHandle_t       g_q;
static volatile uint32_t   g_n_sent    = 0;
static volatile uint32_t   g_n_dropped = 0;
static volatile bool       g_up        = false;

// ---------- BLE callback ----------

class ScanCB : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice *d) override {
        Msg m{};
        strncpy(m.mac, d->getAddress().toString().c_str(), sizeof(m.mac) - 1);
        m.rssi      = (int8_t)d->getRSSI();
        m.addr_type = d->getAddressType();
        m.t_ms      = millis();

        if (d->haveName()) {
            const std::string &raw = d->getName();
            size_t out = 0;
            for (char c : raw) {
                if (out >= sizeof(m.name) - 1) break;
                if (c >= 0x20 && c < 0x7f && c != '"' && c != '\\') m.name[out++] = c;
            }
        }

        if (d->haveManufacturerData()) {
            std::string mstr = d->getManufacturerData();
            size_t n = mstr.size() < 16 ? mstr.size() : 16;
            for (size_t i = 0; i < n; i++) {
                snprintf(&m.manuf[i * 2], 3, "%02x", (unsigned char)mstr[i]);
            }
        }

        // Non-blocking. If the queue is full, drop. (The uplink task is too
        // slow only when the network is down — at which point we'd accumulate
        // forever anyway, so dropping is correct.)
        if (xQueueSend(g_q, &m, 0) != pdTRUE) {
            g_n_dropped++;
        }
    }
};

// ---------- uplink task ----------

static WiFiClient g_tcp;

static bool uplink_connect() {
    if (g_tcp.connected()) return true;
    Serial.printf("[uplink] connecting to %s:%d...\n",
                  MOCKINGBIRD_COLLECTOR_HOST, MOCKINGBIRD_COLLECTOR_PORT);
    if (!g_tcp.connect(MOCKINGBIRD_COLLECTOR_HOST, MOCKINGBIRD_COLLECTOR_PORT, 5000)) {
        g_up = false;
        return false;
    }
    g_tcp.setNoDelay(true);

    char hello[160];
    int n = snprintf(hello, sizeof(hello),
                     "{\"event\":\"hello\",\"leaf\":\"%s\",\"version\":\"%s\"}\n",
                     deviceHostname().c_str(), FW_VERSION);
    g_tcp.write((const uint8_t *)hello, n);
    g_up = true;
    Serial.println("[uplink] connected, hello sent");
    return true;
}

static void uplink_task(void * /*pv*/) {
    Msg m;
    uint32_t last_hb_ms = 0;
    char line[512];

    for (;;) {
        if (!g_tcp.connected()) {
            g_up = false;
            g_tcp.stop();
            if (!uplink_connect()) {
                vTaskDelay(pdMS_TO_TICKS(3000));
                continue;
            }
        }

        if (xQueueReceive(g_q, &m, pdMS_TO_TICKS(500)) == pdTRUE) {
            int n = snprintf(
                line, sizeof(line),
                "{\"event\":\"obs\",\"mac\":\"%s\",\"rssi\":%d,\"addr_type\":%u,"
                "\"name\":\"%s\",\"manuf\":\"%s\",\"t_ms\":%lu}\n",
                m.mac, m.rssi, (unsigned)m.addr_type,
                m.name, m.manuf, (unsigned long)m.t_ms);
            int wrote = g_tcp.write((const uint8_t *)line, n);
            if (wrote == n) {
                g_n_sent++;
            } else {
                g_n_dropped++;
                g_tcp.stop();  // force reconnect on next loop
            }
        }

        uint32_t now = millis();
        if (now - last_hb_ms > 5000) {
            last_hb_ms = now;
            int n = snprintf(
                line, sizeof(line),
                "{\"event\":\"hb\",\"up_s\":%lu,\"n_sent\":%lu,\"n_dropped\":%lu,"
                "\"q_depth\":%d,\"heap\":%u,\"rssi\":%d}\n",
                (unsigned long)(now / 1000),
                (unsigned long)g_n_sent, (unsigned long)g_n_dropped,
                (int)uxQueueMessagesWaiting(g_q),
                (unsigned)ESP.getFreeHeap(),
                WiFi.RSSI());
            g_tcp.write((const uint8_t *)line, n);
        }
    }
}

// ---------- HTTP handlers ----------

static void handleStatus() {
    char body[480];
    snprintf(body, sizeof(body),
             "{\"hostname\":\"%s\",\"version\":\"%s\","
             "\"uptime_s\":%lu,\"rssi\":%d,\"free_heap\":%u,"
             "\"ip\":\"%s\",\"mac\":\"%s\","
             "\"uplink\":{\"connected\":%s,\"sent\":%lu,\"dropped\":%lu,"
             "\"q_depth\":%d,\"host\":\"%s:%d\"}}",
             deviceHostname().c_str(), FW_VERSION,
             (unsigned long)(millis() / 1000UL),
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(),
             WiFi.localIP().toString().c_str(),
             WiFi.macAddress().c_str(),
             g_up ? "true" : "false",
             (unsigned long)g_n_sent, (unsigned long)g_n_dropped,
             (int)uxQueueMessagesWaiting(g_q),
             MOCKINGBIRD_COLLECTOR_HOST, MOCKINGBIRD_COLLECTOR_PORT);
    server.send(200, "application/json", body);
}

static void handleVersion() { server.send(200, "text/plain", FW_VERSION); }

static void handleRestart() {
    server.send(200, "application/json", "{\"status\":\"restarting\"}");
    delay(200);
    ESP.restart();
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
    Serial.printf("\n[wifi] %s rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

static void startOTA() {
    ArduinoOTA.setHostname(deviceHostname().c_str());
    ArduinoOTA.onStart([] {
        Serial.println("[ota] start");
        if (NimBLEDevice::getScan()->isScanning()) {
            NimBLEDevice::getScan()->stop();
        }
        g_tcp.stop();
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

static void startBLE() {
    NimBLEDevice::init("");
    auto *pScan = NimBLEDevice::getScan();
    pScan->setAdvertisedDeviceCallbacks(new ScanCB(), /*wantDup=*/true);
    pScan->setActiveScan(true);
    pScan->setInterval(100);
    pScan->setWindow(99);
    pScan->setMaxResults(0);
    pScan->start(0, nullptr, false);
    Serial.println("[ble] scanner running");
}

// ---------- main ----------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n=== mockingbird leaf %s booted, version %s ===\n",
                  deviceHostname().c_str(), FW_VERSION);

    g_q = xQueueCreate(MSG_QUEUE_LEN, sizeof(Msg));
    if (!g_q) {
        Serial.println("[!] failed to create msg queue");
        delay(2000);
        ESP.restart();
    }

    connectWiFi();

    if (!MDNS.begin(deviceHostname().c_str())) {
        Serial.println("[mdns] FAILED to start");
    } else {
        MDNS.addService("http", "tcp", 80);
        MDNS.addService("mockingbird", "tcp", 80);
    }

    startOTA();
    startBLE();

    // Uplink task on core 1 (Arduino loop is core 1 too — fine, NimBLE uses
    // core 0). Stack 4 KB is plenty for JSON-line formatting + a TCP write.
    xTaskCreatePinnedToCore(uplink_task, "uplink", 4096, nullptr, 1, nullptr, 1);

    server.on("/",        HTTP_GET,  handleStatus);
    server.on("/version", HTTP_GET,  handleVersion);
    server.on("/restart", HTTP_POST, handleRestart);
    server.begin();
    Serial.printf("[http] :80 ready; streaming to %s:%d\n",
                  MOCKINGBIRD_COLLECTOR_HOST, MOCKINGBIRD_COLLECTOR_PORT);
}

void loop() {
    ArduinoOTA.handle();
    server.handleClient();
    delay(1);
}
