# mpy-pod mdns user C module.
#
# espressif__mdns is already compiled and linked via the managed component
# declared in ports/esp32/main/idf_component.yml.  We only need to add
# the header to the include path; the symbols are already in the binary.

add_library(usermod_mdns INTERFACE)

target_sources(usermod_mdns INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modmdns.c
)

target_include_directories(usermod_mdns INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
)

if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    # Locate the managed component's include directory without pulling in
    # its full cmake target (which would drag in IDF component graph).
    idf_build_get_property(build_component_dirs BUILD_COMPONENT_DIRS)
    foreach(dir ${build_component_dirs})
        if(dir MATCHES "espressif__mdns")
            target_include_directories(usermod_mdns INTERFACE "${dir}/include")
            break()
        endif()
    endforeach()
endif()

target_link_libraries(usermod INTERFACE usermod_mdns)
