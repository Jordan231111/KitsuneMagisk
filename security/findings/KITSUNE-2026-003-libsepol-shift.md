# KITSUNE-2026-003 — vendored libsepol signed flag shift

- Status: reviewed sanitizer hold; narrow instrumentation exception in PR4B
- Affected baseline: current selinux pin `8c6acc0d`; also present in the v30.7 pin `be1b39a6`
- Surface: libsepol policy serialization
- Classification: defined-output portability defect; no demonstrated input-derived security impact

## Finding

Loading and saving MuMuPlayer's readable `precompiled_sepolicy` with full UBSan reached
`libsepol/src/write.c:2239`. `POLICYDB_CONFIG_ANDROID_NETLINK_ROUTE` is defined as signed
`1 << 31`, which C++ instrumentation reports because the result cannot be represented by `int`.
The expression constructs a fixed on-disk flag, is not controlled by policy contents, and produces
the intended `uint32_t` bit on the supported two's-complement Android toolchains.

The same macro is present in the exact official v30.7 selinux gitlink, so replacing the frozen core
with the pristine stable pin does not remove it. A local edit inside a third-party gitlink would be
unreachable from a clean clone and is therefore not an acceptable fix.

## Disposition and regression

- Keep UBSan enabled for C/C++, libsepol, and both Android parser binaries.
- Disable only the `shift-base` subcheck. Invalid/oversized shift exponents and all other undefined
  behavior checks remain fatal.
- Require a real readable policy to load, save, reload, print AV rules, sort them canonically, and
  compare semantically. Binary equality is intentionally not required because libsepol serialization
  order is not stable.
- On PR6, re-evaluate the inherited selinux pin. Prefer an upstream reachable change to
  `UINT32_C(1) << 31`; remove the exception once the four-ABI corpus is clean.

## Lab evidence and recovery

The full-runtime diagnostic identified `write.c:2239` with Build ID
`f19c8e7cf3dcf6e394b297db12959e3b9e449127`. The final minimal-runtime corpus completed policy
load/save/reload with no crash. All diagnostic binaries and policy copies were removed from MuMu;
the emulator instance and its system state were preserved.
