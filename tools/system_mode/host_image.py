#!/usr/bin/env python3
"""Stage a current Kitsune payload in a COPY of a RootMac-managed initramfs.

This tool never overwrites its input or publishes a live host image. The external
bootstrap controller owns instance selection, shutdown, publication and restore.
No vendor image or external bootstrap implementation is distributed here.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.next_system_baseline import elf_identity_and_alignments

MAX_IMAGE_SIZE = 256 * 1024 * 1024
BOOTSTRAP = Path(__file__).with_name("host_image_boot.sh")


@dataclass
class Entry:
    name: str
    fields: list[int]
    data: bytes


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def align(value: int) -> int:
    return (value + 3) & ~3


def decode(data: bytes) -> list[Entry]:
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as archive:
            data = archive.read(MAX_IMAGE_SIZE + 1)
    if len(data) > MAX_IMAGE_SIZE:
        raise ValueError("initramfs exceeds the supported size")
    entries, names, position = [], set(), 0
    while position + 110 <= len(data):
        header = data[position:position + 110]
        if header[:6] != b"070701":
            raise ValueError("only newc initramfs archives are supported")
        fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        size, name_size = fields[6], fields[11]
        start = position + 110
        if not 1 <= name_size <= 4096 or start + name_size > len(data):
            raise ValueError("invalid cpio name size")
        raw_name = data[start:start + name_size]
        if raw_name[-1:] != b"\0" or b"\0" in raw_name[:-1]:
            raise ValueError("invalid cpio name")
        name = raw_name[:-1].decode("utf-8")
        while name.startswith("./"):
            name = name[2:]
        if not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
            raise ValueError("unsafe cpio path")
        if str(PurePosixPath(name)) != name or fields[12] != 0:
            raise ValueError("noncanonical newc entry")
        if name in names:
            raise ValueError("duplicate cpio path")
        names.add(name)
        start = align(start + name_size)
        if start + size > len(data):
            raise ValueError("truncated cpio entry")
        if name == "TRAILER!!!":
            if size or any(data[align(start + size):]):
                raise ValueError("unexpected data after cpio trailer")
            return entries
        entries.append(Entry(name, fields, data[start:start + size]))
        position = align(start + size)
    raise ValueError("missing cpio trailer")


def encode(entries: list[Entry]) -> bytes:
    result = bytearray()
    trailer = Entry("TRAILER!!!", [0] * 13, b"")
    for entry in [*entries, trailer]:
        name = entry.name.encode() + b"\0"
        fields = entry.fields.copy()
        fields[6], fields[11], fields[12] = len(entry.data), len(name), 0
        result.extend(b"070701" + b"".join(f"{v:08x}".encode() for v in fields))
        result.extend(name)
        result.extend(bytes(-len(result) % 4))
        result.extend(entry.data)
        result.extend(bytes(-len(result) % 4))
    return gzip.compress(bytes(result), mtime=0)


def payload(apk: Path) -> tuple[dict[str, bytes], dict]:
    if apk.is_symlink() or not apk.is_file() or apk.stat().st_size > MAX_IMAGE_SIZE:
        raise ValueError("APK must be one bounded regular file")
    apk_bytes = apk.read_bytes()
    mapping = {f"lib/arm64-v8a/lib{name}.so": name for name in
               ("busybox", "magisk", "magiskboot", "magiskinit", "magiskpolicy")}
    mapping.update({f"assets/{name}": name for name in
                    ("stub.apk", "util_functions.sh", "boot_patch.sh", "addon.d.sh", "uninstaller.sh")})
    with zipfile.ZipFile(io.BytesIO(apk_bytes)) as archive:
        if (sum(info.file_size for info in archive.infolist()) > MAX_IMAGE_SIZE
                or len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip()):
            raise ValueError("ambiguous or corrupt APK")
        info = json.loads(archive.read("assets/build-info.json"))
        if not isinstance(info, dict):
            raise ValueError("invalid build identity")
        if (info.get("source_dirty") is not False or type(info.get("protocol_version")) is not int
                or info["protocol_version"] != 1
                or not isinstance(info.get("source_commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", info.get("source_commit", ""))
                or info.get("application_id") != "io.github.jordan231111.magisk"):
            raise ValueError("a clean Kitsune Next protocol-v1 artifact is required")
        files = {name: archive.read(source) for source, name in mapping.items()}
        if "lib/armeabi-v7a/libmagisk.so" in archive.namelist():
            files["magisk32"] = archive.read("lib/armeabi-v7a/libmagisk.so")
    binaries = {name: (2, 183, 16384) for name in
                ("busybox", "magisk", "magiskboot", "magiskinit", "magiskpolicy")}
    if "magisk32" in files:
        # Android's 32-bit runtime uses 4 KiB pages.
        binaries["magisk32"] = (1, 40, 4096)
    for name, (expected_class, expected_machine, alignment) in binaries.items():
        data = files[name]
        elf_class, machine, alignments = elf_identity_and_alignments(data)
        if ((elf_class, machine) != (expected_class, expected_machine)
                or data[5] != 1 or min(alignments) < alignment):
            raise ValueError(f"{name} has an invalid ARM ELF ABI or load alignment")
    if any(not value for value in files.values()):
        raise ValueError("empty APK payload")
    files["config"] = b"RECOVERYMODE=false\n"
    files["magisk.apk"] = apk_bytes
    files[".kitsune-host-image.json"] = (json.dumps({
        "schema": 1, "apk_sha256": sha256(files["magisk.apk"]), "build": info,
    }, sort_keys=True) + "\n").encode()
    return files, info


def stage(original: bytes, files: dict[str, bytes], bootstrap: bytes) -> bytes:
    entries = decode(original)
    existing = {entry.name: entry for entry in entries}
    for name in ("boot/bsr", "boot/bsr/payload"):
        entry = existing.get(name)
        if (not entry or stat.S_IFMT(entry.fields[1]) != stat.S_IFDIR
                or entry.fields[2:4] != [0, 0] or entry.fields[1] & 0o022):
            raise ValueError("missing owned bootstrap directory")
    for name in ("boot/stage2.sh", "boot/bsr/early.sh", "boot/bsr/magisk_boot.sh", "boot/bsr/magisk.rc"):
        entry = existing.get(name)
        if not entry or stat.S_IFMT(entry.fields[1]) != stat.S_IFREG or entry.fields[4] != 1:
            raise ValueError("unsupported bootstrap file layout")
    if (b"BlueStacksRootMac managed hook BEGIN" not in existing["boot/stage2.sh"].data
            or b"/data/adb/.bsr_root" not in existing["boot/bsr/early.sh"].data
            or b"/data/local/tmp/.bsr_root_request" not in existing["boot/bsr/early.sh"].data):
        raise ValueError("the external per-instance bootstrap must already be installed")
    entries = [entry for entry in entries if not entry.name.startswith("boot/bsr/payload/")]
    existing["boot/bsr/magisk_boot.sh"].data = bootstrap
    manifest = "\n".join(sorted(files)) + "\n"
    next_inode = max(entry.fields[0] for entry in entries) + 1
    for name, data in sorted({**files, ".bsr_manifest": manifest.encode()}.items()):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name in (".", ".."):
            raise ValueError("invalid payload member")
        if next_inode > 0xffffffff:
            raise ValueError("cpio inode range exhausted")
        mode = 0o644 if name.endswith((".apk", ".json")) or name == "config" else 0o700
        fields = [next_inode, stat.S_IFREG | mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, 0, 0]
        entries.append(Entry("boot/bsr/payload/" + name, fields, data))
        next_inode += 1
    result = encode(entries)
    check = decode(result)
    def contents(entry: Entry):
        return entry.name, entry.fields[:6], entry.fields[7:11], entry.data
    if list(map(contents, check)) != list(map(contents, entries)):
        raise ValueError("staged initramfs failed round-trip verification")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    if (args.input.is_symlink() or not args.input.is_file()
            or args.input.stat().st_size > MAX_IMAGE_SIZE):
        raise ValueError("input must be a regular copied image")
    original = args.input.read_bytes()
    if sha256(original) != args.expected_sha256:
        raise ValueError("input differs from the recorded image")
    files, info = payload(args.apk)
    bootstrap = BOOTSTRAP.read_bytes()
    result = stage(original, files, bootstrap)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(result)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"schema": 1, "route": "host-initramfs", "source_sha256": sha256(original),
                      "staged_sha256": sha256(result), "apk_sha256": sha256(files["magisk.apk"]),
                      "bootstrap_sha256": sha256(bootstrap),
                      "build": info, "published": False}, indent=2))


if __name__ == "__main__":
    main()
