cmake_minimum_required(VERSION 3.22.1)
project(zygisk_test LANGUAGES CXX)

add_library(zygisk_test SHARED zygisk_test.cpp)
target_include_directories(
    zygisk_test
    PRIVATE ../../../../../native/src/core/zygisk
)
target_compile_features(zygisk_test PRIVATE cxx_std_20)
target_compile_options(
    zygisk_test
    PRIVATE -fvisibility=hidden -fno-exceptions -fno-rtti
)
target_link_options(zygisk_test PRIVATE -Wl,--gc-sections)
