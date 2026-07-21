# KitsuneMagisk maintainer guide

This file is the short operational guide. The authoritative technical detail and acceptance criteria
are in [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md); current public claims are in
[`docs/status.md`](docs/status.md).

## How to use the roadmap

Work through the **Proposed pull-request sequence** in order. For each PR, use the relevant P0/P1/P2
section as its detailed specification and test checklist. The priority sections explain *what must be
true*; the PR sequence explains *how to land the work in reviewable increments*.

Do not try to complete every checkbox before starting PR 1. Do not combine several numbered PRs into
one large upstream merge.

## Branch roles

- `kitsune`: current working System Mode reference. Keep the name. Apply only tests and fixes needed
  for another release from this line.
- `next-system`: v30.7-based forward port. Port System Mode before Hide/SuList, external Zygisk, or
  early-mount work.
- official Magisk `master`: observation and selective-backport source, not the first port base.

When `next-system` passes the documented parity gate, promote it to `kitsune` and preserve the old
implementation as an annotated tag. Do not maintain two permanent product lines.

## Current engineering facts

- The current version label is a compatibility value, not proof of a Magisk 31 core.
- `95a048f0` repaired checkout of the existing 2023 SELinux object; it did not update SELinux.
- `25fa2159` fixes normal DenyList interoperability for fresh/reselected configurations, but needs a
  transactional migration for existing `hidelist` data.
- Official Magisk still contains built-in Zygisk. This fork lineage removed it; external provider and
  SuList support must be qualified by exact provider version.
- Generic AVD smoke tests cover normal Magisk boot integration. They do not qualify persistent
  Direct-System on LDPlayer, MuMu, Nox, or BlueStacks.

## Build and test

```sh
export ANDROID_SDK_ROOT=/path/to/android-sdk
git submodule update --init --recursive
./build.py ndk
./build.py all
./build.py -r all
```

The GitHub workflow runs source/static checks, both build variants, JVM test tasks, and the existing
API 23/29/35 AVD smoke matrix on pull requests. Normal pushes do not publish a stable release. A
canary can be published only by manually dispatching the workflow with publication enabled, after
the aggregate product gate passes.

## Review rule

For boot, init, mount, database, or SELinux changes, require a reproducible failing case and a test
that distinguishes the old behavior from the proposed behavior. Compilation alone is not evidence
that System Mode survives a cold boot or can restore the original image.
