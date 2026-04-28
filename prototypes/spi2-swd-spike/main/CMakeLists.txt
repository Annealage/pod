idf_component_register(
    SRCS "spi2_swd_spike.c"
    INCLUDE_DIRS "."
    REQUIRES esp_driver_spi esp_driver_gpio esp_timer driver esp_hw_support
    PRIV_REQUIRES esp_hw_support
)
