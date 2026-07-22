APP_BUILD_SCRIPT := src/Android.mk
APP_ABI          := armeabi-v7a arm64-v8a x86 x86_64
APP_CFLAGS       := -Wall -Oz -fomit-frame-pointer -flto
# Gradle sets NDK_DEBUG=1 for debug variants, which disables ndk-build's
# default section garbage collection. Our Rust archives intentionally use
# panic=abort/no C++ exceptions; retaining their otherwise-dead unwind helpers
# makes ARMv7 pull libunwind and fail on dl_unwind_find_exidx with ONDK r27.1.
# Keep the canonical build.py and Gradle link semantics aligned.
APP_LDFLAGS      := -flto -Wl,--gc-sections
APP_CPPFLAGS     := -std=c++20
APP_STL          := none
APP_PLATFORM     := android-23
APP_THIN_ARCHIVE := true
APP_STRIP_MODE   := none
APP_SUPPORT_FLEXIBLE_PAGE_SIZES := true

# Busybox should use stock libc.a
ifdef B_BB
APP_PLATFORM     := android-26
ifeq ($(OS),Windows_NT)
APP_SHORT_COMMANDS := true
endif
endif
