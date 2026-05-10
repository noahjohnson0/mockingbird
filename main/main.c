/**
 * esp32-fw — general-purpose ESP32-S3 firmware
 *
 *   WiFi STA  →  MicroLink (Tailscale)  →  HTTP control + OTA endpoint
 *
 * Once the device joins the tailnet, you can hit it from any peer:
 *
 *   curl http://<device>.<tailnet>.ts.net/                 # status
 *   curl -X POST http://<device>.<tailnet>.ts.net/ota \
 *        -H "X-OTA-Token: <secret>" \
 *        -d '{"url":"http://100.x.y.z:8000/esp32-fw.bin"}'
 */

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "microlink.h"

#include "http_server.h"
#include "ota.h"
#include "wifi.h"

static const char *TAG = "fw_main";

/* Exposed to http_server.c via `extern` so /status can report Tailscale state. */
microlink_t *g_ml = NULL;

static void on_ml_state(microlink_t *ml, microlink_state_t state, void *user_data) {
    static const char *names[] = {
        "IDLE", "WIFI_WAIT", "CONNECTING", "REGISTERING",
        "CONNECTED", "RECONNECTING", "ERROR",
    };
    const char *n = state < (sizeof(names) / sizeof(names[0])) ? names[state] : "?";
    ESP_LOGI(TAG, "MicroLink state -> %s", n);

    if (state == ML_STATE_CONNECTED) {
        char ip[16];
        microlink_ip_to_str(microlink_get_vpn_ip(ml), ip);
        ESP_LOGI(TAG, "tailnet up — VPN IP %s", ip);

        /* If we got here on a fresh OTA boot, confirm the new image. */
        fw_ota_mark_self_ok();
    }
}

void app_main(void) {
    /* NVS — required for WiFi calibration and MicroLink key storage. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    ESP_LOGI(TAG, "esp32-fw boot — free heap %u, PSRAM free %u",
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    /* 1. WiFi up first — MicroLink expects an active interface. */
    ESP_ERROR_CHECK(fw_wifi_start_and_wait());

    /* 2. Start the HTTP server now so the device is reachable on the LAN
     * IP even before Tailscale registration completes. The same handlers
     * answer on the Tailscale IP once MicroLink is connected. */
    httpd_handle_t httpd = NULL;
    ESP_ERROR_CHECK(fw_http_server_start(&httpd));

    /* 3. Bring up Tailscale. */
    microlink_config_t cfg = {
        .auth_key = CONFIG_ML_TAILSCALE_AUTH_KEY,
        .device_name = CONFIG_ML_DEVICE_NAME,
        .enable_derp = true,
        .enable_stun = true,
        .enable_disco = true,
        .max_peers = CONFIG_ML_MAX_PEERS,
        .wifi_tx_power_dbm = 13,
    };
    g_ml = microlink_init(&cfg);
    if (!g_ml) {
        ESP_LOGE(TAG, "microlink_init failed");
        return;
    }
    microlink_set_state_callback(g_ml, on_ml_state, NULL);
    ESP_ERROR_CHECK(microlink_start(g_ml));

    /* 4. Idle. State transitions and HTTP requests are handled by their
     * own tasks; the main task just stays alive. */
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(60000));
    }
}
