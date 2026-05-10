idf_component_register(
    SRCS
        "main.c"
        "wifi.c"
        "http_server.c"
        "ota.c"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
        esp_http_server
        esp_https_ota
        app_update
        microlink
)
