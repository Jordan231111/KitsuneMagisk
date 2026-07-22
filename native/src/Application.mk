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

# Security-lab builds instrument the C/C++ and libsepol parser surfaces. Rust
# archives remain panic=abort and are exercised by the same deterministic
# device corpus. Keep this opt-in so release artifacts cannot accidentally
# inherit sanitizer runtime dependencies.
ifdef KITSUNE_SANITIZE
# The minimal static compiler-rt works with Magisk's static executables and
# keeps the lab binaries on the same API 23 ABI floor as release artifacts.
# libsepol intentionally uses signed 1 << 31 for an on-disk Android flag in
# both this pin and v30.7. Suppress only signed shift-base overflow; invalid
# shift exponents and every other undefined-behavior check remain enabled.
APP_CFLAGS       := -Wall -O1 -g -fno-omit-frame-pointer -flto -DKITSUNE_SANITIZE_BUILD=1 -fsanitize=$(KITSUNE_SANITIZE) -fno-sanitize=shift-base -fsanitize-minimal-runtime -fno-sanitize-recover=all
APP_LDFLAGS      := -flto -Wl,--gc-sections -fsanitize=$(KITSUNE_SANITIZE) -fsanitize-minimal-runtime -static-libsan -fno-sanitize-recover=all
endif

# Busybox should use stock libc.a
ifdef B_BB
APP_PLATFORM     := android-26
ifeq ($(OS),Windows_NT)
APP_SHORT_COMMANDS := true
endif
endif
