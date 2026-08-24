#!/usr/bin/env python3
"""Verify an inherited next-system source and APK contract without network access."""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Any, Iterable, Optional
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "next-system-baseline.json"
PT_LOAD = 1
CERT_PATTERN = re.compile(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})")
PACKAGE_PATTERN = re.compile(
    r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'",
    re.MULTILINE,
)
SDK_PATTERN = re.compile(r"^sdkVersion:'([^']+)'$", re.MULTILINE)
TARGET_SDK_PATTERN = re.compile(r"^targetSdkVersion:'([^']+)'$", re.MULTILINE)
LABEL_PATTERN = re.compile(r"^application-label:'([^']+)'$", re.MULTILINE)
UTILITY_VERSION_PATTERN = re.compile(rb"(?m)^MAGISK_VER='([^'\r\n]+)'\r?$")
UTILITY_CODE_PATTERN = re.compile(rb"(?m)^MAGISK_VER_CODE=([0-9]+)\r?$")
DAEMON_MODE_PATTERN = re.compile(
    rb"(?<![0-9A-Za-z._+-])([0-9A-Za-z._+-]+):MAGISK:([DR])\x00"
)
DAEMON_FULL_VERSION_PATTERN = re.compile(
    rb"(?<![0-9A-Za-z._+-])([0-9A-Za-z._+-]+)\(([0-9]+)\)\x00"
)
VERSION_PATTERN = re.compile(r"^30\.7-kitsune-next\.[0-9a-f]{8}$")
ABI_ELF_IDENTITIES = {
    "armeabi-v7a": (1, 40),
    "arm64-v8a": (2, 183),
    "x86": (1, 3),
    "x86_64": (2, 62),
}
REQUIRED_LIBRARIES = {
    abi: {
        "libbusybox.so",
        "libinit-ld.so",
        "libmagisk.so",
        "libmagiskboot.so",
        "libmagiskinit.so",
        "libmagiskpolicy.so",
    }
    for abi in ABI_ELF_IDENTITIES
}
REQUIRED_INSTALL_ENTRIES = {
    "META-INF/com/google/android/update-binary",
    "META-INF/com/google/android/updater-script",
    "assets/addon.d.sh",
    "assets/app_functions.sh",
    "assets/boot_patch.sh",
    "assets/stub.apk",
    "assets/uninstaller.sh",
    "assets/util_functions.sh",
}


class BaselineError(ValueError):
    pass


def run(command: Iterable[object], cwd: Path = ROOT, check: bool = True) -> str:
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and result.returncode != 0:
        rendered = " ".join(str(item) for item in command)
        raise BaselineError(f"command failed ({result.returncode}): {rendered}\n{result.stdout}")
    return result.stdout


def merge_manifest(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_manifest(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def read_manifest(
    path: Path, loading: tuple[Path, ...] = ()
) -> dict[str, Any]:
    path = path.resolve()
    if path in loading:
        chain = " -> ".join(str(item) for item in (*loading, path))
        raise BaselineError(f"baseline manifest inheritance cycle: {chain}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline manifest {path}: {exc}") from exc
    if data.get("schema") != 1:
        raise BaselineError("unsupported baseline manifest schema")
    parent = data.pop("extends", None)
    if parent is not None:
        if not isinstance(parent, str) or not parent or Path(parent).is_absolute():
            raise BaselineError("baseline manifest extends must be a relative path")
        parent_path = (path.parent / parent).resolve()
        data = merge_manifest(read_manifest(parent_path, (*loading, path)), data)
    return data


def expect_text(root: Path, relative: str, expected: str) -> None:
    try:
        data = (root / relative).read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot inspect {relative}: {exc}") from exc
    if expected not in data:
        raise BaselineError(f"{relative} is missing required source contract: {expected!r}")


def git_lines(root: Path, *args: str) -> list[str]:
    return [line for line in run(("git", *args), root).splitlines() if line]


def source_delta(root: Path, upstream: str) -> list[str]:
    changed = set(git_lines(root, "diff", "--name-only", upstream, "--"))
    changed.update(git_lines(root, "ls-files", "--others", "--exclude-standard"))
    return sorted(changed)


def cargo_lock_packages(path: Path) -> set[tuple[str, str]]:
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot read Cargo lockfile {path}: {exc}") from exc
    packages: set[tuple[str, str]] = set()
    for block in contents.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"$', block, re.MULTILINE)
        version = re.search(r'^version = "([^"]+)"$', block, re.MULTILINE)
        if name is None or version is None:
            raise BaselineError(f"cannot parse Cargo package block in {path}")
        packages.add((name.group(1), version.group(1)))
    if not packages:
        raise BaselineError(f"Cargo lockfile contains no packages: {path}")
    return packages


def verify_dependency_contract(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    contract = manifest.get("dependency_contract")
    if contract is None:
        return {"enforced": False}
    packages = cargo_lock_packages(root / "native" / "src" / "Cargo.lock")
    required = {
        (entry["name"], entry["version"]) for entry in contract.get("required", [])
    }
    forbidden = {
        (entry["name"], entry["version"]) for entry in contract.get("forbidden", [])
    }
    missing = sorted(required - packages)
    present = sorted(forbidden & packages)
    if missing or present:
        raise BaselineError(
            "native dependency contract failed; "
            f"missing_required={missing}, present_forbidden={present}"
        )
    return {
        "enforced": True,
        "required": sorted(f"{name}@{version}" for name, version in required),
        "forbidden_absent": sorted(
            f"{name}@{version}" for name, version in forbidden
        ),
    }


def verify_source(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    phase = manifest.get("phase", "PR6")
    upstream = manifest["upstream"]["commit"]
    run(("git", "cat-file", "-e", f"{upstream}^{{commit}}"), root)
    run(("git", "merge-base", "--is-ancestor", upstream, "HEAD"), root)

    delta = source_delta(root, upstream)
    allowed = sorted(manifest["source_delta"])
    unexpected = sorted(set(delta) - set(allowed))
    missing = sorted(set(allowed) - set(delta))
    if unexpected or missing:
        raise BaselineError(
            f"source delta does not match the reviewed {phase} allowlist; "
            f"unexpected={unexpected}, missing={missing}"
        )

    tracked = set(git_lines(root, "ls-files"))
    forbidden = sorted(set(manifest["forbidden_paths"]) & tracked)
    if forbidden:
        raise BaselineError(f"{phase} contains forbidden feature paths: {forbidden}")

    submodules: dict[str, dict[str, Any]] = {}
    for path, expected in manifest["submodules"].items():
        actual = run(("git", "rev-parse", f":{path}"), root).strip()
        if actual != expected:
            raise BaselineError(
                f"submodule gitlink changed for {path}: expected {expected}, found {actual}"
            )
        worktree = root / path
        initialized = (worktree / ".git").exists() or (worktree / ".git").is_file()
        if initialized:
            checkout = run(("git", "rev-parse", "HEAD"), worktree).strip()
            if checkout != expected:
                raise BaselineError(
                    f"submodule checkout mismatch for {path}: expected {expected}, found {checkout}"
                )
            dirty = run(
                ("git", "status", "--porcelain", "--untracked-files=no"),
                worktree,
            ).strip()
            if dirty:
                raise BaselineError(f"submodule has tracked local changes: {path}")
        submodules[path] = {"gitlink": actual, "initialized": initialized}

    identity = manifest["identity"]
    expected_contracts = {
        "app/buildSrc/src/main/java/Plugin.kt": [
            f'const val APP_ID = "{identity["application_id"]}"',
            f'const val PRODUCT_CHANNEL = "{identity["channel"]}"',
            f'const val UPSTREAM_BASE = "{upstream}"',
            'const val VERSION_PREFIX = "30.7-kitsune-next"',
            ".findGitDir(rootFile(\".\"))",
        ],
        "build.py": [
            'error("Requires Python 3.9+")',
            'f"30.7-kitsune-next.{commit_hash}"',
        ],
        "native/src/include/consts.hpp": [identity["application_id"]],
        "native/src/include/consts.rs": [identity["application_id"]],
        "app/core/src/main/java/com/topjohnwu/magisk/core/Config.kt": [
            "preference(Key.CHECK_UPDATES, false)"
        ],
        "app/core/src/main/java/com/topjohnwu/magisk/core/Const.kt": [
            identity["source_url"]
        ],
        "app/core/src/main/res/values/resources.xml": [identity["display_name"]],
        "app/test/build.gradle.kts": [
            'applicationId = "$APP_ID.test"',
            'manifestPlaceholders["magiskTestAppId"] = "$APP_ID.test"',
        ],
        "app/test/src/main/AndroidManifest.xml": [
            "${magiskAppId}",
            "${magiskTestAppId}",
        ],
        "scripts/test_common.sh": [
            f'MAGISK_APP_PACKAGE="${{MAGISK_APP_PACKAGE:-{identity["application_id"]}}}"',
            'test-${variant}.apk',
        ],
        "native/src/core/Cargo.toml": [
            'default = ["check-signature", "check-client", "su-check-db"]'
        ],
        "native/src/core/package.rs": [
            '#[cfg(all(feature = "check-signature", not(debug_assertions)))]'
        ],
    }
    for path, values in expected_contracts.items():
        for value in values:
            expect_text(root, path, value)

    # Ordinary upstream installation and built-in Zygisk surfaces must remain present.
    required_paths = {
        "app/core/src/main/java/com/topjohnwu/magisk/core/tasks/MagiskInstaller.kt",
        "native/src/core/zygisk/mod.rs",
        "scripts/boot_patch.sh",
        "scripts/flash_script.sh",
        "scripts/live_setup.sh",
        "scripts/uninstaller.sh",
    }
    absent = sorted(required_paths - tracked)
    if absent:
        raise BaselineError(f"ordinary upstream installation surface is incomplete: {absent}")

    return {
        "phase": phase,
        "upstream_commit": upstream,
        "source_delta": delta,
        "submodules": submodules,
        "identity": identity,
        "signature_enforcement": True,
        "ordinary_install_surface": True,
        "built_in_zygisk": True,
        "dependencies": verify_dependency_contract(root, manifest),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def version_key(path: Path) -> tuple[int, ...]:
    parts = re.findall(r"[0-9]+", path.name)
    return tuple(int(part) for part in parts)


def find_build_tool(name: str, explicit: Optional[Path]) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise BaselineError(f"Android build tool does not exist: {explicit}")
    sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if not sdk_root:
        raise BaselineError(f"set ANDROID_HOME or pass --{name}")
    candidates = sorted(
        (Path(sdk_root) / "build-tools").glob(f"*/{name}"),
        key=lambda item: version_key(item.parent),
        reverse=True,
    )
    if not candidates:
        raise BaselineError(f"cannot find {name} below {sdk_root}/build-tools")
    return candidates[0]


def signer_certificate(apksigner: Path, apk: Path) -> str:
    output = run((apksigner, "verify", "--print-certs", apk))
    certificates = sorted({item.lower() for item in CERT_PATTERN.findall(output)})
    if len(certificates) != 1:
        raise BaselineError(
            f"{apk} must contain exactly one signer certificate, found {certificates}"
        )
    return certificates[0]


def zip_entry_data_offset(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    stream = archive.fp
    if stream is None:
        raise BaselineError("APK ZIP is closed")
    stream.seek(info.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise BaselineError(f"invalid ZIP local header for {info.filename}")
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    return info.header_offset + 30 + name_length + extra_length


def elf_identity_and_alignments(data: bytes) -> tuple[int, int, list[int]]:
    if len(data) < 6 or data[:4] != b"\x7fELF":
        raise BaselineError("packaged native library is not an ELF file")
    elf_class = data[4]
    byte_order = data[5]
    if byte_order == 1:
        endian = "<"
    elif byte_order == 2:
        endian = ">"
    else:
        raise BaselineError("unsupported ELF byte order")
    if elf_class == 1:
        if len(data) < 52:
            raise BaselineError("truncated ELF32 header")
        phoff = struct.unpack_from(f"{endian}I", data, 28)[0]
        phentsize = struct.unpack_from(f"{endian}H", data, 42)[0]
        phnum = struct.unpack_from(f"{endian}H", data, 44)[0]
        minimum_size, align_offset, align_format = 32, 28, "I"
    elif elf_class == 2:
        if len(data) < 64:
            raise BaselineError("truncated ELF64 header")
        phoff = struct.unpack_from(f"{endian}Q", data, 32)[0]
        phentsize = struct.unpack_from(f"{endian}H", data, 54)[0]
        phnum = struct.unpack_from(f"{endian}H", data, 56)[0]
        minimum_size, align_offset, align_format = 56, 48, "Q"
    else:
        raise BaselineError("unsupported ELF class")
    machine = struct.unpack_from(f"{endian}H", data, 18)[0]
    if phnum in (0, 0xFFFF) or phentsize < minimum_size:
        raise BaselineError("invalid ELF program-header table")
    if phoff + phentsize * phnum > len(data):
        raise BaselineError("truncated ELF program-header table")
    alignments = []
    for index in range(phnum):
        offset = phoff + index * phentsize
        if struct.unpack_from(f"{endian}I", data, offset)[0] == PT_LOAD:
            alignments.append(
                struct.unpack_from(
                    f"{endian}{align_format}", data, offset + align_offset
                )[0]
            )
    if not alignments:
        raise BaselineError("ELF file has no loadable segments")
    return elf_class, machine, alignments


def parse_utility_identity(data: bytes) -> tuple[str, int]:
    versions = UTILITY_VERSION_PATTERN.findall(data)
    codes = UTILITY_CODE_PATTERN.findall(data)
    if len(versions) != 1 or len(codes) != 1:
        raise BaselineError("util_functions.sh has an ambiguous build identity")
    return versions[0].decode("ascii"), int(codes[0])


def parse_daemon_identity(data: bytes) -> tuple[str, int, str]:
    modes = {
        (match.group(1).decode("ascii"), match.group(2).decode("ascii"))
        for match in DAEMON_MODE_PATTERN.finditer(data)
    }
    full_versions = {
        (match.group(1).decode("ascii"), int(match.group(2)))
        for match in DAEMON_FULL_VERSION_PATTERN.finditer(data)
    }
    if len(modes) != 1 or len(full_versions) != 1:
        raise BaselineError(
            "Magisk daemon has ambiguous identities: "
            f"modes={modes}, full_versions={full_versions}"
        )
    version, mode = modes.pop()
    full_version, code = full_versions.pop()
    if version != full_version:
        raise BaselineError(
            f"Magisk daemon version strings disagree: {version} != {full_version}"
        )
    return version, code, mode


def inspect_apk(
    apk: Path,
    variant: str,
    aapt: Path,
    apksigner: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if not apk.is_file():
        raise BaselineError(f"missing {variant} APK: {apk}")
    badging = run((aapt, "dump", "badging", apk))
    package_match = PACKAGE_PATTERN.search(badging)
    sdk_match = SDK_PATTERN.search(badging)
    target_match = TARGET_SDK_PATTERN.search(badging)
    label_match = LABEL_PATTERN.search(badging)
    if not all((package_match, sdk_match, target_match, label_match)):
        raise BaselineError(f"cannot parse APK metadata from {apk}")
    assert package_match is not None
    assert sdk_match is not None
    assert target_match is not None
    assert label_match is not None
    package_name, version_code, version_name = package_match.groups()
    identity = manifest["identity"]
    if package_name != identity["application_id"]:
        raise BaselineError(f"unexpected manager package in {apk}: {package_name}")
    if int(version_code) != identity["version_code"]:
        raise BaselineError(f"unexpected versionCode in {apk}: {version_code}")
    if not VERSION_PATTERN.fullmatch(version_name):
        raise BaselineError(f"untruthful experimental versionName in {apk}: {version_name}")
    if sdk_match.group(1) != str(manifest["android"]["min_sdk"]):
        raise BaselineError(f"unexpected minSdk in {apk}: {sdk_match.group(1)}")
    if target_match.group(1) != str(manifest["android"]["target_sdk"]):
        raise BaselineError(f"unexpected targetSdk in {apk}: {target_match.group(1)}")
    if label_match.group(1) != identity["display_name"]:
        raise BaselineError(f"unexpected application label in {apk}: {label_match.group(1)}")

    expected_mode = "D" if variant == "debug" else "R"
    native: dict[str, list[int]] = {}
    daemon_identities: dict[str, tuple[str, int, str]] = {}
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        duplicates = sorted(
            name for name, count in Counter(names).items() if count > 1
        )
        if duplicates:
            raise BaselineError(f"{apk} has duplicate ZIP entries: {duplicates}")
        required_entries = REQUIRED_INSTALL_ENTRIES | set(
            manifest.get("additional_install_entries", [])
        )
        missing_entries = sorted(required_entries - set(names))
        if missing_entries:
            raise BaselineError(f"{apk} is missing install entries: {missing_entries}")
        utility_version, utility_code = parse_utility_identity(
            archive.read("assets/util_functions.sh")
        )
        if (utility_version, utility_code) != (version_name, int(version_code)):
            raise BaselineError(
                f"manager/native utility identity mismatch in {apk}: "
                f"{utility_version}/{utility_code}"
            )
        for name in sorted(item for item in names if item.startswith("lib/") and item.endswith(".so")):
            parts = Path(name).parts
            if len(parts) != 3 or parts[1] not in ABI_ELF_IDENTITIES:
                raise BaselineError(f"unexpected native ABI path in {apk}: {name}")
            abi = parts[1]
            info = archive.getinfo(name)
            offset = zip_entry_data_offset(archive, info)
            if info.compress_type == zipfile.ZIP_STORED and offset % 16384 != 0:
                raise BaselineError(f"{apk}:{name} is not 16 KiB ZIP aligned")
            data = archive.read(info)
            elf_class, machine, alignments = elf_identity_and_alignments(data)
            if (elf_class, machine) != ABI_ELF_IDENTITIES[abi]:
                raise BaselineError(
                    f"{apk}:{name} has wrong ELF identity {elf_class}/{machine} for {abi}"
                )
            # Android supports 16 KiB pages only for its 64-bit ABIs. The NDK
            # intentionally retains 4 KiB ELF alignment for 32-bit ABIs.
            minimum_elf_alignment = 16384 if abi in {"arm64-v8a", "x86_64"} else 4096
            invalid = [
                value
                for value in alignments
                if value < minimum_elf_alignment or value % minimum_elf_alignment
            ]
            if invalid:
                raise BaselineError(f"{apk}:{name} has invalid PT_LOAD alignment: {invalid}")
            native[name] = alignments
            if Path(name).name == "libmagisk.so":
                daemon_identities[abi] = parse_daemon_identity(data)
        for abi, libraries in REQUIRED_LIBRARIES.items():
            packaged = {
                Path(name).name for name in native if name.startswith(f"lib/{abi}/")
            }
            missing_libraries = sorted(libraries - packaged)
            if missing_libraries:
                raise BaselineError(f"{apk} is missing {abi} libraries: {missing_libraries}")
        expected_daemon = (version_name, int(version_code), expected_mode)
        if set(daemon_identities.values()) != {expected_daemon}:
            raise BaselineError(
                f"{apk} daemon identities do not match {expected_daemon}: {daemon_identities}"
            )
        try:
            comment = archive.comment.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BaselineError(f"{apk} has a non-UTF-8 identity comment") from exc
        if f"version={version_name}\n" not in comment or f"versionCode={version_code}\n" not in comment:
            raise BaselineError(f"{apk} ZIP comment does not match its manifest identity")

    certificate = signer_certificate(apksigner, apk)
    return {
        "path": str(apk.resolve()),
        "sha256": sha256(apk),
        "size": apk.stat().st_size,
        "package": package_name,
        "version_code": int(version_code),
        "version_name": version_name,
        "min_sdk": int(sdk_match.group(1)),
        "target_sdk": int(target_match.group(1)),
        "label": label_match.group(1),
        "certificate_sha256": certificate,
        "daemon_mode": expected_mode,
        "abis": sorted(REQUIRED_LIBRARIES),
        "native_library_count": len(native),
        "zip_16k_aligned": True,
        "elf_alignment_valid_for_abi": True,
    }


def inspect_test_apk(apk: Path, aapt: Path, apksigner: Path, app_id: str) -> dict[str, Any]:
    if not apk.is_file():
        raise BaselineError(f"missing test APK: {apk}")
    badging = run((aapt, "dump", "badging", apk))
    package_match = PACKAGE_PATTERN.search(badging)
    test_app_id = f"{app_id}.test"
    if package_match is None or package_match.group(1) != test_app_id:
        raise BaselineError(f"unexpected instrumentation package in {apk}")
    manifest_tree = run((aapt, "dump", "xmltree", apk, "AndroidManifest.xml"))
    if (
        "android:targetPackage" not in manifest_tree
        or f'="{app_id}"' not in manifest_tree
        or f'="{test_app_id}"' not in manifest_tree
    ):
        raise BaselineError(
            f"instrumentation APK does not target {app_id} and {test_app_id}"
        )
    return {
        "path": str(apk.resolve()),
        "sha256": sha256(apk),
        "size": apk.stat().st_size,
        "package": package_match.group(1),
        "target_package": app_id,
        "self_target_package": test_app_id,
        "certificate_sha256": signer_certificate(apksigner, apk),
    }


def verify_artifacts(
    outdir: Path,
    manifest: dict[str, Any],
    aapt: Path,
    apksigner: Path,
) -> dict[str, Any]:
    debug = inspect_apk(outdir / "app-debug.apk", "debug", aapt, apksigner, manifest)
    release = inspect_apk(outdir / "app-release.apk", "release", aapt, apksigner, manifest)
    test_debug = inspect_test_apk(
        outdir / "test-debug.apk",
        aapt,
        apksigner,
        manifest["identity"]["application_id"],
    )
    test_release = inspect_test_apk(
        outdir / "test-release.apk",
        aapt,
        apksigner,
        manifest["identity"]["application_id"],
    )
    if release["certificate_sha256"] in manifest["forbidden_release_signers"]:
        raise BaselineError("release APK uses a forbidden public/debug signer")
    if release["certificate_sha256"] == debug["certificate_sha256"]:
        raise BaselineError("debug and release APKs must use different signing identities")
    if test_debug["certificate_sha256"] != debug["certificate_sha256"]:
        raise BaselineError("debug instrumentation APK does not match the debug manager signer")
    if test_release["certificate_sha256"] != release["certificate_sha256"]:
        raise BaselineError("release instrumentation APK does not match the release manager signer")
    return {
        "debug": debug,
        "release": release,
        "test_debug": test_debug,
        "test_release": test_release,
        "signer_separation": True,
        "instrumentation_signers_match": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", action="store_true", help="verify the reviewed source delta")
    parser.add_argument(
        "--artifacts", type=Path, help="verify app and signer-matched test APKs"
    )
    parser.add_argument("--aapt", type=Path)
    parser.add_argument("--apksigner", type=Path)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.source and args.artifacts is None:
        raise BaselineError("select --source and/or --artifacts")
    root = args.root.resolve()
    manifest = read_manifest(args.manifest.resolve())
    report: dict[str, Any] = {"schema": 1, "passed": True}
    if args.source:
        report["source"] = verify_source(root, manifest)
    if args.artifacts is not None:
        aapt = find_build_tool("aapt", args.aapt)
        apksigner = find_build_tool("apksigner", args.apksigner)
        report["artifacts"] = verify_artifacts(
            args.artifacts.resolve(), manifest, aapt, apksigner
        )
        report["android_tools"] = {
            "aapt": str(aapt),
            "apksigner": str(apksigner),
        }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaselineError as exc:
        print(f"next-system baseline verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
