#!/usr/bin/env python3
"""Test the APK's resetprop in private copies of Android property areas."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import uuid
import zipfile


SCRIPT = r'''
set -eu
export PROP_LAB=$1 PROP_MAGISK=$2 PROP_BUSYBOX=$3 PROP_KEY=$4
[ ! -e "$PROP_LAB" ]
mkdir -m 700 "$PROP_LAB"
mkdir "$PROP_LAB/primary"
"$PROP_BUSYBOX" cp -a /dev/__properties__/. "$PROP_LAB/primary/"
if [ ! -d "$PROP_LAB/primary/appcompat_override" ]; then
  "$PROP_BUSYBOX" cp -a /dev/__properties__ "$PROP_LAB/override"
  mv "$PROP_LAB/override" "$PROP_LAB/primary/appcompat_override"
fi
"$PROP_BUSYBOX" unshare -m "$PROP_BUSYBOX" sh -c '
  set -eu
  "$PROP_BUSYBOX" mount --make-rprivate /
  "$PROP_BUSYBOX" mount -o bind "$PROP_LAB/primary" /dev/__properties__
  key=ro.appcompat_override.$PROP_KEY
  "$PROP_MAGISK" resetprop -n "$PROP_KEY" base
  "$PROP_MAGISK" resetprop -n "$key" first
  "$PROP_MAGISK" resetprop -n "$key" second
  test "$(/system/bin/getprop "$PROP_KEY")" = base
  test "$(/system/bin/getprop "$key")" = second
  echo short-override-update
  long=$("$PROP_BUSYBOX" printf "%0128d" 0)
  "$PROP_MAGISK" resetprop -n "$key" "$long"
  test "$(/system/bin/getprop "$key")" = "$long"
  "$PROP_MAGISK" resetprop -n "$key" ""
  test -z "$("$PROP_MAGISK" resetprop "$key")"
  "$PROP_MAGISK" resetprop -n "$key" final
  test "$(/system/bin/getprop "$key")" = final
  echo long-empty-short-transitions
  "$PROP_MAGISK" resetprop -n "$PROP_KEY.normal" before
  "$PROP_MAGISK" resetprop -n "$PROP_KEY.normal" after
  test "$(/system/bin/getprop "$PROP_KEY.normal")" = after
  echo ordinary-update
  "$PROP_BUSYBOX" mount -o bind "$PROP_LAB/primary/appcompat_override" /dev/__properties__
  test "$(/system/bin/getprop "$PROP_KEY")" = final
  test "$(/system/bin/getprop "$PROP_KEY.normal")" = after
  echo override-reader-view
'
'''


def qualify(adb: str, serial: str, apk: Path) -> dict:
    command = [adb, "-s", serial]

    def run(*args: str) -> str:
        result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"property fixture failed ({result.returncode}): "
                               + result.stdout + result.stderr)
        return result.stdout.strip()

    def root(script: str) -> str:
        return run("shell", "PATH=$PATH:/sbin:/debug_ramdisk; su -c " + shlex.quote(script))

    nonce = uuid.uuid4().hex
    guest = "/data/local/tmp/kitsune-properties-" + nonce
    key = "ro.kitsune.pr15.p" + nonce
    keys = [key, "ro.appcompat_override." + key, key + ".normal"]
    before = [run("shell", "getprop", prop) for prop in keys]
    if any(before):
        raise RuntimeError("property fixture names already exist")
    abi = run("shell", "getprop", "ro.product.cpu.abi")
    if abi not in {"arm64-v8a", "armeabi-v7a", "x86", "x86_64"}:
        raise ValueError("unsupported property fixture ABI")
    if root("id -u") != "0":
        raise RuntimeError("property fixtures require an authorized root shell")
    run("shell", "umask 077; mkdir " + guest)
    try:
        with tempfile.TemporaryDirectory(prefix="kitsune-properties-") as directory:
            work = Path(directory)
            with zipfile.ZipFile(apk) as archive:
                build = json.loads(archive.read("assets/build-info.json"))
                for name in ("magisk", "busybox"):
                    path = work / name
                    path.write_bytes(archive.read(f"lib/{abi}/lib{name}.so"))
                    run("push", str(path), guest + "/" + name)
            script = work / "probe.sh"
            script.write_text(SCRIPT)
            run("push", str(script), guest + "/probe.sh")
            run("shell", f"chmod 755 {guest}/magisk {guest}/busybox")
            output = root(shlex.join([guest + "/busybox", "sh", guest + "/probe.sh",
                                      guest + "/areas", guest + "/magisk",
                                      guest + "/busybox", key]))
        expected = {"short-override-update", "long-empty-short-transitions",
                    "ordinary-update", "override-reader-view"}
        if set(output.splitlines()) != expected:
            raise RuntimeError("incomplete property fixture output: " + output)
        if [run("shell", "getprop", prop) for prop in keys] != before:
            raise RuntimeError("property fixture escaped its private namespace")
        return {"schema": 1, "kind": "isolated-appcompat-property-fixtures",
                "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "build": build,
                "fingerprint": run("shell", "getprop", "ro.build.fingerprint"),
                "checks": {name: "passed" for name in sorted(expected)},
                "outside_namespace_unchanged": True,
                "scope": "Actual resetprop and platform readers on cloned property areas; "
                         "not an OEM appcompat rollout claim."}
    finally:
        root("rm -rf " + guest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = qualify(args.adb, args.serial, args.apk)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("Private property override fixtures passed")


if __name__ == "__main__":
    main()
