// mockingbird ESP32-WROOM-32 leaf firmware.
//
//   WiFi STA (Mockingbird)  →  ArduinoOTA listener  +  small HTTP server
//
// The ESP32 joins the mockingbird WiFi, announces itself via mDNS as
// `mockingbird-<chipid>.local`, and listens for:
//
//   • ArduinoOTA on port 3232 (push-based) — for `pio run -t upload` from
//     any device that can reach the mockingbird LAN (Mac on Mockingbird, or
//     any tailnet peer via the Pi's subnet route at 192.168.8.0/24).
//
//   • HTTP on port 80 with these endpoints:
//       GET  /            — status JSON (version, uptime, RSSI, free heap, MAC)
//       GET  /version     — plain text version
//       POST /restart     — reboot
//
// Designed so the only manual flash is the very first one (USB). All
// subsequent updates flow over WiFi.

#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <esp_system.h>

#include "secrets.h"

static const char *FW_VERSION = MOCKINGBIRD_FW_VERSION;

// `mockingbird-<6-hex>` — last 3 bytes of the WiFi MAC, lowercase, no separators.
static String deviceHostname() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char buf[24];
    snprintf(buf, sizeof(buf), "mockingbird-%02x%02x%02x", mac[3], mac[4], mac[5]);
    return String(buf);
}

static WebServer server(80);

// ---------- HTTP handlers ----------

static void handleStatus() {
    char body[320];
    snprintf(body, sizeof(body),
        "{\"hostname\":\"%s\",\"version\":\"%s\","
         "\"uptime_s\":%lu,\"rssi\":%d,\"free_heap\":%u,"
         "\"ip\":\"%s\",\"mac\":\"%s\"}",
        deviceHostname().c_str(), FW_VERSION,
        (unsigned long)(millis() / 1000UL),
        WiFi.RSSI(), (unsigned)ESP.getFreeHeap(),
        WiFi.localIP().toString().c_str(),
        WiFi.macAddress().c_str());
    server.send(200, "application/json", body);
}

static void handleVersion() {
    server.send(200, "text/plain", FW_VERSION);
}

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
    Serial.printf("\n[wifi] connected: %s  rssi=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

static void startOTA() {
    ArduinoOTA.setHostname(deviceHostname().c_str());
    // Auth: left disabled while we're only on the mockingbird LAN behind WPA2+
    // Tailscale ACLs. Add ArduinoOTA.setPassword("...") if exposing wider.

    ArduinoOTA.onStart([] {
        Serial.printf("[ota] start: %s\n",
                      ArduinoOTA.getCommand() == U_FLASH ? "flash" : "spiffs");
    });
    ArduinoOTA.onEnd([] { Serial.println("\n[ota] end"); });
    ArduinoOTA.onProgress([](unsigned int p, unsigned int t) {
        Serial.printf("[ota] %u%%\r", (p * 100) / t);
    });
    ArduinoOTA.onError([](ota_error_t e) {
        Serial.printf("[ota] error %u\n", e);
    });

    ArduinoOTA.begin();
    Serial.printf("[ota] listening as %s.local (port 3232)\n",
                  deviceHostname().c_str());
}

// ---------- main ----------

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n=== mockingbird leaf %s booted, version %s ===\n",
                  deviceHostname().c_str(), FW_VERSION);

    connectWiFi();

    // mDNS lets the host be found as `mockingbird-XXXXXX.local` for OTA + HTTP.
    if (!MDNS.begin(deviceHostname().c_str())) {
        Serial.println("[mdns] FAILED to start");
    } else {
        MDNS.addService("http", "tcp", 80);
        MDNS.addService("mockingbird", "tcp", 80);  // discovery hint
    }

    startOTA();

    server.on("/",        HTTP_GET,  handleStatus);
    server.on("/version", HTTP_GET,  handleVersion);
    server.on("/restart", HTTP_POST, handleRestart);
    server.begin();
    Serial.println("[http] listening on :80");
}

void loop() {
    ArduinoOTA.handle();
    server.handleClient();
    delay(1);
}
