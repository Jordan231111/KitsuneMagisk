# KITSUNE-2026-002 — truncated boot headers reached unchecked alignment

- Status: fixed with a device regression in PR4B
- Affected baseline: `f943ecddd11d0e648877ffb1f917c16767b8f7cc`
- Surface: `magiskboot unpack` boot-image parser
- Classification: local denial of service and parser hardening defect

## Finding

An eight-byte file containing only `ANDROID!` was accepted as an AOSP boot-header candidate. The
parser copied fields beyond the logical end of the file, observed a zero page size, and passed it to
the alignment helper. An ARM64 UBSan build on MuMuPlayer port 16384 aborted with
`divrem-overflow`. Adjacent offset arithmetic also added untrusted block sizes before proving that
the resulting aligned range remained inside the mapped image.

This is not represented as a remote code-execution finding. A caller must already cause
`magiskboot` to process a chosen local image. The demonstrated impact is a deterministic process
abort; unchecked pointer/size arithmetic on a privileged parser still warrants a complete bound fix.

## Minimal proof

```text
printf 'ANDROID!' > truncated.img
magiskboot unpack -n truncated.img
```

Before the fix, the sanitizer reported a division overflow after printing `PAGESIZE [0]`. The exact
eight-byte input remains in the deterministic boot corpus.

## Fix and regression

- Pass the remaining mapped length—not the whole file length—while scanning format signatures.
- Reject truncated v0–v4, vendor, PXA, shifted-loader, and AMONET headers before copying them.
- Reject zero/oversized page sizes and use overflow-safe, map-bounded block alignment.
- Bound MTK, zImage, DTB, and AVB follow-on reads, including the logical kernel tail and first DTB
  node, and release a rejected temporary header.
- Bound fixed-width header names before rendering them as text.
- Require boot unpack/repack/sign/verify to return zero; malformed cases may reject cleanly but may
  not signal, abort, or emit a sanitizer marker.
- Rerun the seed plus 64 mutations per seed on explicit MuMu serial `127.0.0.1:16384` and in the
  disposable API 35 ARM64 AVD sanitizer lane: each target completed 977 cases with zero crashes,
  including boot sign/verify and policy save/reload.

## Recovery and retirement

The proof touched only a unique `/data/local/tmp/kitsune-security-*` sandbox, which was removed.
No boot partition or MuMu instance was modified. Carry the seed and checked-offset implementation
across PR6; retire only when the inherited parser rejects the same corpus and passes all-ABI
sign/verify gates.
