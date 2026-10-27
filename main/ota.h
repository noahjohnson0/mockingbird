#pragma once

#include "esp_err.h"

/**
 * Pull a new firmware image from `url` and apply it.
 *
 * On success this function does not return — the device reboots into the
 * freshly-written OTA slot. The new image starts in the "pending-verify"
 * state; if it fails to call fw_ota_mark_self_ok() within the boot
 * watchdog, the bootloader rolls back to the previous slot.
 *
 * `url` may be http:// or https://. When called from a peer on the
 * tailnet, http:// is fine because WireGuard already encrypts the link.
 */
esp_err_t fw_ota_pull_and_apply(const char *url);

/**
 * Confirm that this boot is healthy.
 *
 * Call this after the app has come up cleanly (WiFi up, Tailscale
 * registered, etc). On the first boot after an OTA it cancels the
 * pending rollback; on every subsequent boot it's a no-op.
 */
void fw_ota_mark_self_ok(void);
