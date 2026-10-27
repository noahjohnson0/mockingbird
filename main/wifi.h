#pragma once

#include "esp_err.h"

/**
 * Bring up WiFi STA and block until we have an IP.
 *
 * Uses the credentials baked into Kconfig (CONFIG_ML_WIFI_SSID /
 * CONFIG_ML_WIFI_PASSWORD). Auto-reconnects on disconnect.
 */
esp_err_t fw_wifi_start_and_wait(void);
