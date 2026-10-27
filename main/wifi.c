#include "wifi.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static const char *TAG = "fw_wifi";

#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_event_group;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = data;
        ESP_LOGW(TAG, "disconnected (reason=%d), retrying", d->reason);
        xEventGroupClearBits(s_event_group, WIFI_CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&e->ip_info.ip));
        xEventGroupSetBits(s_event_group, WIFI_CONNECTED_BIT);
    }
}

esp_err_t fw_wifi_start_and_wait(void) {
    s_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID, &on_event, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP, &on_event, NULL, NULL));

    wifi_config_t wifi_config = {
        .sta = {.threshold.authmode = WIFI_AUTH_WPA2_PSK},
    };
    strncpy((char *)wifi_config.sta.ssid, CONFIG_ML_WIFI_SSID,
            sizeof(wifi_config.sta.ssid) - 1);
    strncpy((char *)wifi_config.sta.password, CONFIG_ML_WIFI_PASSWORD,
            sizeof(wifi_config.sta.password) - 1);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* MicroLink/WireGuard wants low latency — power save adds DTIM stalls. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "connecting to \"%s\"...", CONFIG_ML_WIFI_SSID);
    xEventGroupWaitBits(s_event_group, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE,
                        portMAX_DELAY);
    return ESP_OK;
}
