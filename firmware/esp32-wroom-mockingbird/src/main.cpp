// mockingbird ESP32-WROOM-32 leaf firmware (BLE → TCP streamer, v0.3.1-stream).
//
// v0.3.1 additions over v0.3.0:
//   • NVS-persisted location label, settable via POST /location.
//     /status reports it, the uplink hello + every obs include it.
//   • Non-blocking TCP write: we check WiFiClient::availableForWrite()
//     against the line size before send. If the send buffer is full
//     (== peer dead or backpressured), we drop the packet and force a
//     reconnect rather than block the uplink task — which is what
//     wedged 4ce184 for 2 hours during the Pi swap window.
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
#include <Preferences.h>
#include <esp_system.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

#include "secrets.h"

static const char *FW_VERSION = MOCKINGBIRD_FW_VERSION;

#ifndef MOCKINGBIRD_COLLECTOR_HOST
#define MOCKINGBIRD_COLLECTOR_HOST "192.168.8.202"
#endif
#ifndef MOCKINGBIRD_COLLECTOR_PORT
#define MOCKINGBIRD_COLLECTOR_PORT 9001
#endif
#ifndef MOCKINGBIRD_COLLECTOR_MDNS_HOST
// Bare hostname (no .local) — avahi on the Pi publishes "mockingbird-pi.local".
#define MOCKINGBIRD_COLLECTOR_MDNS_HOST "mockingbird-pi"
#endif
#ifndef MOCKINGBIRD_MDNS_TIMEOUT_MS
#define MOCKINGBIRD_MDNS_TIMEOUT_MS 5000
#endif

static String deviceHostname() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    char buf[24];
    snprintf(buf, sizeof(buf), "mockingbird-%02x%02x%02x", mac[3], mac[4], mac[5]);
    return String(buf);
}

static WebServer server(80);

// ---------- location label (NVS-persisted) ----------

static Preferences g_prefs;
static char        g_location[32] = {0};

static void load_location() {
    g_prefs.begin("mockingbird", /*readOnly=*/true);
    String s = g_prefs.getString("location", "");
    g_prefs.end();
    strncpy(g_location, s.c_str(), sizeof(g_location) - 1);
}

static void save_location(const char *s) {
    strncpy(g_location, s, sizeof(g_location) - 1);
    g_location[sizeof(g_location) - 1] = '\0';
    g_prefs.begin("mockingbird", /*readOnly=*/false);
    g_prefs.putString("location", g_location);
    g_prefs.end();
}

// ---------- streaming message queue ----------

struct Msg {
    char     mac[18];     // "AA:BB:CC:DD:EE:FF\0"
    int8_t   rssi;
    uint8_t  addr_type;
    uint8_t  ch;          // primary advertising channel index: 37/38/39, or 0 = unknown
    char     name[24];    // truncated/sanitized
    char     manuf[33];   // hex of first 16 mfg-data bytes + NUL
    uint32_t t_ms;
};

// Build-time overridable for A/B testing different buffer depths against
// the BLE+WiFi single-radio coex ceiling. Set via -DMSG_QUEUE_LEN=N in
// platformio.ini or the CLI. 64 is the historic baseline (~3s of headroom
// at 20 obs/s); larger absorbs longer TCP stalls without dropping BLE obs.
#ifndef MSG_QUEUE_LEN
#define MSG_QUEUE_LEN 64
#endif

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

        // Primary advertising channel index (37/38/39).
        //
        // RF context: a BLE advertiser cycles its ADV_IND PDU across the three
        // primary channels (2402/2426/2480 MHz). Per-channel RSSI bias is a
        // real physical effect — antenna gain, package/PCB reflections, and
        // WiFi co-channel interference all differ at those three frequencies.
        // Capturing this per-observation lets calibration cancel it out
        // downstream (Vlad's P0 physical effect; tracked separately —
        // PURU-2 ships the plumbing only, math comes in a follow-up).
        //
        // NimBLE-Arduino 1.4.2 caveat: the legacy onResult() callback path
        // does NOT plumb the primary channel through. `ble_gap_disc_desc`
        // (host/include/host/ble_gap.h) has no channel field, and the host
        // discards the controller's channel hint before invoking us. The
        // HCI LE Advertising Report event itself per Core spec carries no
        // channel index — only the Extended Advertising Report does, and
        // ESP32 (classic, non-S3) controller support for ext-adv reports is
        // patchy.
        //
        // For now we stamp 0 = "unknown" so the schema and downstream
        // collector/DB are wired up. Two real-source options for the
        // follow-up ticket:
        //   1) Patch NimBLE host (ble_hs_hci_evt.c) to stash the controller's
        //      channel hint into the disc_desc, then add a getter on
        //      NimBLEAdvertisedDevice. ~30 LOC, but it's a vendored patch.
        //   2) Move scanning to ESP-IDF host/nimble directly and hook the
        //      raw HCI event — cleaner separation of concerns.
        // Either way, only this assignment changes; the wire format and
        // collector are stable.
        m.ch = 0;

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

// Cached collector IP. Resolved via mDNS query for MOCKINGBIRD_COLLECTOR_MDNS_HOST
// at first connect; falls back to the compile-time MOCKINGBIRD_COLLECTOR_HOST on
// failure. Invalidated on any connect/short-write failure so we re-resolve and
// pick up a Pi that DHCP-flipped its lease.
static IPAddress g_collector_ip((uint32_t)0);
static char      g_collector_src[8] = "none";  // "mdns" or "static"

static void invalidate_collector_ip() {
    g_collector_ip = IPAddress((uint32_t)0);
    g_collector_src[0] = 'n'; g_collector_src[1] = 'o';
    g_collector_src[2] = 'n'; g_collector_src[3] = 'e';
    g_collector_src[4] = 0;
}

static bool resolve_collector() {
    // Try mDNS first. ESP32's MDNS.queryHost() blocks up to `timeout_ms` and
    // returns INADDR_NONE (0.0.0.0) on failure. 5 s is generous; avahi on
    // the Pi typically responds in <100 ms once the leaf is on the AP.
    IPAddress ip = MDNS.queryHost(MOCKINGBIRD_COLLECTOR_MDNS_HOST,
                                  MOCKINGBIRD_MDNS_TIMEOUT_MS);
    if (ip != IPAddress((uint32_t)0)) {
        g_collector_ip = ip;
        strcpy(g_collector_src, "mdns");
        Serial.printf("[uplink] mDNS resolved %s.local -> %s\n",
                      MOCKINGBIRD_COLLECTOR_MDNS_HOST, ip.toString().c_str());
        return true;
    }
    // Fallback: parse the compile-time literal.
    if (g_collector_ip.fromString(MOCKINGBIRD_COLLECTOR_HOST)) {
        strcpy(g_collector_src, "static");
        Serial.printf("[uplink] mDNS miss; falling back to static %s\n",
                      MOCKINGBIRD_COLLECTOR_HOST);
        return true;
    }
    Serial.printf("[uplink] could not resolve collector (mDNS+static both failed)\n");
    return false;
}

static bool uplink_connect() {
    if (g_tcp.connected()) return true;
    if (g_collector_ip == IPAddress((uint32_t)0)) {
        if (!resolve_collector()) {
            g_up = false;
            return false;
        }
    }
    Serial.printf("[uplink] connecting to %s:%d (via %s)...\n",
                  g_collector_ip.toString().c_str(),
                  MOCKINGBIRD_COLLECTOR_PORT, g_collector_src);
    if (!g_tcp.connect(g_collector_ip, MOCKINGBIRD_COLLECTOR_PORT, 5000)) {
        // The cached IP is dead — drop it so the next attempt re-resolves.
        invalidate_collector_ip();
        g_up = false;
        return false;
    }
    g_tcp.setNoDelay(true);

    // 1-second send timeout — write() returns short rather than blocking
    // when the peer is dead-but-not-yet-detected. Combined with the short-
    // write fallback below, this kills the wedge condition.
    int fd = g_tcp.fd();
    if (fd >= 0) {
        struct timeval to = {1, 0};
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &to, sizeof(to));

        // TCP keepalive: detect half-open connections that the application
        // layer can't see. Without these, a leaf can sit happily writing
        // bytes into a dead socket's local TX buffer for minutes before
        // realizing nothing is being delivered. With these, the kernel
        // probes at IDLE seconds of silence, retries every INTVL, gives
        // up after CNT misses → dead connection detected in ~25 s.
        int on = 1;
        int idle = 15;   // seconds of idle before first probe
        int intvl = 5;   // seconds between probes
        int cnt = 2;     // probes before giving up
        setsockopt(fd, SOL_SOCKET,  SO_KEEPALIVE,  &on,    sizeof(on));
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE,  &idle,  sizeof(idle));
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
        setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT,   &cnt,   sizeof(cnt));
    }

    char hello[200];
    int n = snprintf(hello, sizeof(hello),
                     "{\"event\":\"hello\",\"leaf\":\"%s\",\"version\":\"%s\","
                     "\"location\":\"%s\"}\n",
                     deviceHostname().c_str(), FW_VERSION, g_location);
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
                "\"ch\":%u,"
                "\"name\":\"%s\",\"manuf\":\"%s\",\"t_ms\":%lu,"
                "\"location\":\"%s\"}\n",
                m.mac, m.rssi, (unsigned)m.addr_type,
                (unsigned)m.ch,
                m.name, m.manuf, (unsigned long)m.t_ms,
                g_location);

            // Non-blocking-ish guard: if write returns less than full, the
            // socket is backpressured or dead — force reconnect rather
            // than block on subsequent calls. This catches the wedge
            // case (peer dead but not yet detected by TCP keep-alive)
            // because the very next write returns 0.
            int wrote = g_tcp.write((const uint8_t *)line, n);
            if (wrote == n) {
                g_n_sent++;
            } else {
                g_n_dropped++;
                Serial.printf("[uplink] short write %d/%d — reconnect\n", wrote, n);
                g_tcp.stop();
                // Peer may have moved (DHCP flip on the Pi). Force re-resolve.
                invalidate_collector_ip();
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
    char body[560];
    String hostStr = (g_collector_ip == IPAddress((uint32_t)0))
                       ? String(MOCKINGBIRD_COLLECTOR_HOST)
                       : g_collector_ip.toString();
    snprintf(body, sizeof(body),
             "{\"hostname\":\"%s\",\"version\":\"%s\",\"location\":\"%s\","
             "\"uptime_s\":%lu,\"rssi\":%d,\"free_heap\":%u,"
             "\"ip\":\"%s\",\"mac\":\"%s\","
             "\"uplink\":{\"connected\":%s,\"sent\":%lu,\"dropped\":%lu,"
             "\"q_depth\":%d,\"host\":\"%s:%d\",\"host_src\":\"%s\"}}",
             deviceHostname().c_str(), FW_VERSION, g_location,
             (unsigned long)(millis() / 1000UL),
             WiFi.RSSI(), (unsigned)ESP.getFreeHeap(),
             WiFi.localIP().toString().c_str(),
             WiFi.macAddress().c_str(),
             g_up ? "true" : "false",
             (unsigned long)g_n_sent, (unsigned long)g_n_dropped,
             (int)uxQueueMessagesWaiting(g_q),
             hostStr.c_str(), MOCKINGBIRD_COLLECTOR_PORT,
             g_collector_src);
    server.send(200, "application/json", body);
}

static void handleLocationGet() {
    char body[64];
    snprintf(body, sizeof(body), "{\"location\":\"%s\"}", g_location);
    server.send(200, "application/json", body);
}

static void handleLocationPost() {
    // Accept either plain text body or JSON {"label":"..."}.
    String raw = server.arg("plain");
    raw.trim();
    String label;
    if (raw.startsWith("{")) {
        int s = raw.indexOf("\"label\"");
        int colon = (s >= 0) ? raw.indexOf(':', s) : -1;
        int q1 = (colon >= 0) ? raw.indexOf('"', colon) : -1;
        int q2 = (q1 >= 0) ? raw.indexOf('"', q1 + 1) : -1;
        if (q2 > q1) label = raw.substring(q1 + 1, q2);
    } else {
        label = raw;
    }
    if (label.length() == 0 || label.length() >= sizeof(g_location)) {
        server.send(400, "application/json", "{\"error\":\"label empty or too long\"}");
        return;
    }
    // Sanitize: printable ASCII only
    for (size_t i = 0; i < label.length(); i++) {
        char c = label[i];
        if (c < 0x20 || c >= 0x7f || c == '"' || c == '\\') {
            server.send(400, "application/json", "{\"error\":\"bad char\"}");
            return;
        }
    }
    save_location(label.c_str());

    // Force uplink reconnect so the new label propagates immediately in
    // the hello message (and every subsequent obs).
    g_tcp.stop();

    char body[96];
    snprintf(body, sizeof(body), "{\"status\":\"ok\",\"location\":\"%s\"}", g_location);
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
    // Set the device name to our hostname so other leaves see "mockingbird-XXXXXX"
    // in advertising data and can identify us.
    NimBLEDevice::init(deviceHostname().c_str());

    auto *pScan = NimBLEDevice::getScan();
    pScan->setAdvertisedDeviceCallbacks(new ScanCB(), /*wantDup=*/true);
    pScan->setActiveScan(true);
    pScan->setInterval(100);
    pScan->setWindow(99);
    pScan->setMaxResults(0);
    pScan->start(0, nullptr, false);
    Serial.println("[ble] scanner running");

    // ---- Tier-1 calibration: advertise our existence to other leaves ----
    // Each leaf transmits a small BLE advertisement every ~1s containing its
    // hostname. Other leaves' scanners pick it up like any other BLE device,
    // and the collector can identify these "leaf-to-leaf" observations by the
    // name prefix. Since we have known distances between every pair of leaves,
    // these are HIGH-FIDELITY ground-truth (rssi, distance) calibration points.
    auto *pAdv = NimBLEDevice::getAdvertising();
    NimBLEAdvertisementData adv;
    adv.setName(deviceHostname().c_str());  // "mockingbird-XXXXXX"
    // Use company ID 0xFFFF (test/reserved, IEEE-recognized for non-production
    // use) followed by magic "MOCK" so the collector can quickly identify
    // leaf-source observations.
    std::string manuf;
    manuf.push_back((char)0xFF);
    manuf.push_back((char)0xFF);
    manuf += "MOCK";
    adv.setManufacturerData(manuf);
    pAdv->setAdvertisementData(adv);
    // 1s advertising interval (NimBLE uses 0.625ms units; 1600 = 1000ms)
    pAdv->setMinInterval(1600);
    pAdv->setMaxInterval(1600);
    pAdv->start();
    Serial.printf("[ble] advertising as %s (1s interval)\n",
                  deviceHostname().c_str());
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

    load_location();
    Serial.printf("[loc] location=\"%s\"\n", g_location);

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

    server.on("/",         HTTP_GET,  handleStatus);
    server.on("/version",  HTTP_GET,  handleVersion);
    server.on("/restart",  HTTP_POST, handleRestart);
    server.on("/location", HTTP_GET,  handleLocationGet);
    server.on("/location", HTTP_POST, handleLocationPost);
    server.begin();
    Serial.printf("[http] :80 ready; will stream to %s.local:%d "
                  "(fallback %s)\n",
                  MOCKINGBIRD_COLLECTOR_MDNS_HOST,
                  MOCKINGBIRD_COLLECTOR_PORT,
                  MOCKINGBIRD_COLLECTOR_HOST);
}

void loop() {
    ArduinoOTA.handle();
    server.handleClient();
    delay(1);
}
