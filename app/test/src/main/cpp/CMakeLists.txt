cmake_minimum_required(VERSION 3.22.1)
project(zygisk_test LANGUAGES CXX)

add_library(zygisk_test SHARED zygisk_test.cpp)
add_library(zygisk_unload_test SHARED zygisk_test.cpp)
target_compile_definitions(zygisk_unload_test PRIVATE KITSUNE_ZYGISK_TEST_DLCLOSE=1)

foreach(target zygisk_test zygisk_unload_test)
    target_include_directories(
        ${target}
        PRIVATE ../../../../../native/src/core/zygisk
    )
    target_compile_features(${target} PRIVATE cxx_std_20)
    target_compile_options(
        ${target}
        PRIVATE -fvisibility=hidden -fno-exceptions -fno-rtti -fno-threadsafe-statics
    )
    target_link_options(${target} PRIVATE -nostdlib++ -Wl,--gc-sections)
endforeach()
