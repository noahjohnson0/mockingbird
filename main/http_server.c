#include "http_server.h"

#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "microlink.h"

#include "ota.h"

static const char *TAG = "fw_http";

/* Filled in by main.c so /status can report VPN IP and connection state. */
extern microlink_t *g_ml;

/* ---------- auth ---------- */

static bool check_token(httpd_req_t *req) {
    const char *expected = CONFIG_FW_OTA_TOKEN;
    if (!expected[0]) {
        ESP_LOGW(TAG, "CONFIG_FW_OTA_TOKEN is empty — refusing mutating request");
        return false;
    }
    char hdr[128];
    if (httpd_req_get_hdr_value_str(req, "X-OTA-Token", hdr, sizeof(hdr)) != ESP_OK) {
        return false;
    }
    /* constant-time-ish compare; lengths first */
    size_t a = strlen(expected), b = strlen(hdr);
    if (a != b) return false;
    uint8_t diff = 0;
    for (size_t i = 0; i < a; i++) diff |= (uint8_t)(expected[i] ^ hdr[i]);
    return diff == 0;
}

static esp_err_t send_json(httpd_req_t *req, const char *json) {
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, json);
}

/* ---------- GET / ---------- */

static esp_err_t status_get(httpd_req_t *req) {
    const esp_app_desc_t *app = esp_app_get_description();
    uint64_t uptime_s = esp_timer_get_time() / 1000000ULL;

    char vpn_ip_str[16] = "0.0.0.0";
    int peers = 0;
    bool connected = false;
    if (g_ml) {
        connected = microlink_is_connected(g_ml);
        uint32_t ip = microlink_get_vpn_ip(g_ml);
        microlink_ip_to_str(ip, vpn_ip_str);
        peers = microlink_get_peer_count(g_ml);
    }

    char body[384];
    snprintf(body, sizeof(body),
             "{\"version\":\"%s\",\"build\":\"%s %s\","
             "\"uptime_s\":%llu,\"free_heap\":%u,"
             "\"tailscale\":{\"connected\":%s,\"vpn_ip\":\"%s\",\"peers\":%d}}",
             app->version, app->date, app->time,
             (unsigned long long)uptime_s,
             (unsigned)esp_get_free_heap_size(),
             connected ? "true" : "false", vpn_ip_str, peers);
    return send_json(req, body);
}

/* ---------- GET /version ---------- */

static esp_err_t version_get(httpd_req_t *req) {
    const esp_app_desc_t *app = esp_app_get_description();
    httpd_resp_set_type(req, "text/plain");
    return httpd_resp_sendstr(req, app->version);
}

/* ---------- POST /ota ---------- */

static void ota_task(void *arg) {
    char *url = arg;
    fw_ota_pull_and_apply(url);
    /* On success we don't get here — esp_restart() jumps into the new app.
     * On failure, free the buffer and exit. */
    free(url);
    vTaskDelete(NULL);
}

/* Very small body — just extract a "url" value. Avoids pulling in cJSON. */
static bool extract_url(const char *body, char *out, size_t out_sz) {
    const char *p = strstr(body, "\"url\"");
    if (!p) return false;
    p = strchr(p, ':');
    if (!p) return false;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '"') return false;
    p++;
    const char *end = strchr(p, '"');
    if (!end) return false;
    size_t n = (size_t)(end - p);
    if (n >= out_sz) return false;
    memcpy(out, p, n);
    out[n] = '\0';
    return true;
}

static esp_err_t ota_post(httpd_req_t *req) {
    if (!check_token(req)) {
        httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, "bad or missing X-OTA-Token");
        return ESP_OK;
    }

    char buf[512];
    int total = 0;
    while (total < (int)sizeof(buf) - 1) {
        int n = httpd_req_recv(req, buf + total, sizeof(buf) - 1 - total);
        if (n <= 0) break;
        total += n;
    }
    buf[total] = '\0';

    char url[384];
    if (!extract_url(buf, url, sizeof(url))) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "expected JSON: {\"url\":\"...\"}");
        return ESP_OK;
    }

    httpd_resp_set_type(req, "application/json");
    httpd_resp_sendstr(req, "{\"status\":\"ota_started\"}");

    /* Run OTA on a dedicated task so the request handler doesn't block
     * the httpd server for the full download. */
    char *url_copy = strdup(url);
    if (!url_copy) return ESP_OK;
    xTaskCreate(ota_task, "fw_ota", 8192, url_copy, 5, NULL);
    return ESP_OK;
}

/* ---------- POST /restart ---------- */

static void restart_task(void *arg) {
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
}

static esp_err_t restart_post(httpd_req_t *req) {
    if (!check_token(req)) {
        httpd_resp_send_err(req, HTTPD_401_UNAUTHORIZED, "bad or missing X-OTA-Token");
        return ESP_OK;
    }
    send_json(req, "{\"status\":\"restarting\"}");
    xTaskCreate(restart_task, "fw_restart", 2048, NULL, 5, NULL);
    return ESP_OK;
}

/* ---------- registration ---------- */

esp_err_t fw_http_server_start(httpd_handle_t *out_handle) {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = CONFIG_FW_HTTP_PORT;
    cfg.lru_purge_enable = true;
    cfg.stack_size = 8192;

    httpd_handle_t srv = NULL;
    esp_err_t err = httpd_start(&srv, &cfg);
    if (err != ESP_OK) return err;

    httpd_uri_t routes[] = {
        {.uri = "/",         .method = HTTP_GET,  .handler = status_get},
        {.uri = "/version",  .method = HTTP_GET,  .handler = version_get},
        {.uri = "/ota",      .method = HTTP_POST, .handler = ota_post},
        {.uri = "/restart",  .method = HTTP_POST, .handler = restart_post},
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++) {
        httpd_register_uri_handler(srv, &routes[i]);
    }

    if (out_handle) *out_handle = srv;
    ESP_LOGI(TAG, "HTTP server up on :%d", CONFIG_FW_HTTP_PORT);
    return ESP_OK;
}
