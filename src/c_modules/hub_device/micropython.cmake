# Annealage Pod: hub_device user C module.
#
# Vendor-class virtual USB device exposed via USB/IP. Delivers DUT
# connect/disconnect notifications to a host daemon on EP1 IN.

add_library(usermod_hub_device INTERFACE)

target_sources(usermod_hub_device INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modhub_device.c
    ${CMAKE_CURRENT_LIST_DIR}/hub_device.c
)

target_include_directories(usermod_hub_device INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/..
)

target_link_libraries(usermod INTERFACE usermod_hub_device)
