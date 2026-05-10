#pragma once

#include "esp_err.h"
#include "esp_http_server.h"

/**
 * Start the control HTTP server.
 *
 * Listens on CONFIG_FW_HTTP_PORT, bound to 0.0.0.0 — reachable on both
 * the local WiFi IP and the Tailscale IP once MicroLink is up.
 *
 * Endpoints:
 *   GET  /             — JSON status (version, uptime, heap, peers)
 *   GET  /version      — plain text app version
 *   POST /ota          — body: {"url": "..."} — pulls and applies firmware
 *   POST /restart      — reboot the device
 *
 * Mutating endpoints require an `X-OTA-Token: <secret>` header matching
 * CONFIG_FW_OTA_TOKEN.
 */
esp_err_t fw_http_server_start(httpd_handle_t *out_handle);
