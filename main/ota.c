#include "ota.h"

#include <string.h>

#include "esp_app_format.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"

static const char *TAG = "fw_ota";

esp_err_t fw_ota_pull_and_apply(const char *url) {
    if (!url || !url[0]) {
        return ESP_ERR_INVALID_ARG;
    }
    ESP_LOGI(TAG, "starting OTA from %s", url);

    esp_http_client_config_t http_cfg = {
        .url = url,
        .timeout_ms = 30000,
        .keep_alive_enable = true,
        /* For https:// URLs, trust the bundled root CA set. */
        .crt_bundle_attach = esp_crt_bundle_attach,
#ifdef CONFIG_FW_OTA_SKIP_CERT_CHECK
        .skip_cert_common_name_check = true,
#endif
    };
    esp_https_ota_config_t ota_cfg = {
        .http_config = &http_cfg,
    };

    esp_err_t err = esp_https_ota(&ota_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGW(TAG, "OTA applied — rebooting into new slot");
    esp_restart();
    /* not reached */
    return ESP_OK;
}

void fw_ota_mark_self_ok(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return;
    }
    if (state == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGI(TAG, "first boot after OTA looks healthy — confirming");
        esp_ota_mark_app_valid_cancel_rollback();
    }
}
