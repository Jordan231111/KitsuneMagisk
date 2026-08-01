from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from typing import Any
import zipfile


PT_LOAD = 1
ABI_ELF_IDENTITIES = {
    "armeabi-v7a": (1, 40),
    "arm64-v8a": (2, 183),
    "x86": (1, 3),
    "x86_64": (2, 62),
}
CERT_PATTERN = re.compile(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})")
UTILITY_VERSION_PATTERN = re.compile(rb"(?m)^MAGISK_VER='([^'\r\n]+)'\r?$")
UTILITY_CODE_PATTERN = re.compile(rb"(?m)^MAGISK_VER_CODE=([0-9]+)\r?$")
DAEMON_VERSION_PATTERN = re.compile(
    rb"(?<![0-9A-Za-z._+-])([0-9A-Za-z._+-]+):MAGISK:([DR]) \(([0-9]+)\)\x00"
)
REQUIRED_APP_LIBRARIES = {
    "arm64-v8a": {
        "libbusybox.so", "libmagisk64.so", "libmagiskboot.so",
        "libmagiskinit.so", "libmagiskpolicy.so",
    },
    "armeabi-v7a": {
        "libbusybox.so", "libmagisk32.so", "libmagiskboot.so",
        "libmagiskinit.so", "libmagiskpolicy.so",
    },
    "x86": {
        "libbusybox.so", "libmagisk32.so", "libmagiskboot.so",
        "libmagiskinit.so", "libmagiskpolicy.so",
    },
    "x86_64": {
        "libbusybox.so", "libmagisk64.so", "libmagiskboot.so",
        "libmagiskinit.so", "libmagiskpolicy.so",
    },
}
RECOVERY_SCRIPT_PATH = "META-INF/com/google/android/updater-script"
ADDON_SCRIPT_PATH = "assets/addon.d.sh"
SYSTEM_MODE_MANAGER_PATH = "res/raw/manager.sh"


class ArtifactContractError(ValueError):
    pass


def elf_identity_and_load_alignments(data: bytes) -> tuple[int, int, list[int]]:
    if len(data) < 6 or data[:4] != b"\x7fELF":
        raise ArtifactContractError("not an ELF file")
    elf_class = data[4]
    byte_order = data[5]
    if byte_order == 1:
        endian = "<"
    elif byte_order == 2:
        endian = ">"
    else:
        raise ArtifactContractError("unsupported ELF byte order")

    if elf_class == 1:
        if len(data) < 52:
            raise ArtifactContractError("truncated ELF header")
        phoff = struct.unpack_from(f"{endian}I", data, 28)[0]
        phentsize = struct.unpack_from(f"{endian}H", data, 42)[0]
        phnum = struct.unpack_from(f"{endian}H", data, 44)[0]
        minimum_entry_size = 32
        align_offset = 28
        align_format = "I"
    elif elf_class == 2:
        if len(data) < 64:
            raise ArtifactContractError("truncated ELF header")
        phoff = struct.unpack_from(f"{endian}Q", data, 32)[0]
        phentsize = struct.unpack_from(f"{endian}H", data, 54)[0]
        phnum = struct.unpack_from(f"{endian}H", data, 56)[0]
        minimum_entry_size = 56
        align_offset = 48
        align_format = "Q"
    else:
        raise ArtifactContractError("unsupported ELF class")

    machine = struct.unpack_from(f"{endian}H", data, 18)[0]

    if phnum == 0 or phnum == 0xFFFF or phentsize < minimum_entry_size:
        raise ArtifactContractError("invalid or unsupported ELF program-header table")
    table_end = phoff + phentsize * phnum
    if table_end > len(data):
        raise ArtifactContractError("truncated ELF program-header table")

    alignments: list[int] = []
    for index in range(phnum):
        offset = phoff + index * phentsize
        segment_type = struct.unpack_from(f"{endian}I", data, offset)[0]
        if segment_type == PT_LOAD:
            alignments.append(
                struct.unpack_from(f"{endian}{align_format}", data, offset + align_offset)[0]
            )
    if not alignments:
        raise ArtifactContractError("ELF file has no loadable segments")
    return elf_class, machine, alignments


def elf_load_alignments(data: bytes) -> list[int]:
    return elf_identity_and_load_alignments(data)[2]


def zip_entry_data_offset(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    stream = archive.fp
    if stream is None:
        raise ArtifactContractError("APK ZIP is closed")
    stream.seek(info.header_offset)
    header = stream.read(30)
    if len(header) != 30 or header[:4] != b"PK\x03\x04":
        raise ArtifactContractError(f"invalid ZIP local header for {info.filename}")
    name_length, extra_length = struct.unpack_from("<HH", header, 26)
    return info.header_offset + 30 + name_length + extra_length


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def signer_certificates(apksigner: Path, apk: Path) -> list[str]:
    result = subprocess.run(
        [str(apksigner), "verify", "--print-certs", str(apk)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if result.returncode != 0:
        raise ArtifactContractError(f"apksigner rejected {apk}: {result.stdout.strip()}")
    certificates = sorted({match.lower() for match in CERT_PATTERN.findall(result.stdout)})
    if len(certificates) != 1:
        raise ArtifactContractError(
            f"{apk} must have exactly one signer certificate, found {len(certificates)}"
        )
    return certificates


def missing_required_libraries(native: dict[str, list[int]]) -> dict[str, list[str]]:
    packaged = {
        abi: {
            Path(name).name
            for name in native
            if name.startswith(f"lib/{abi}/")
        }
        for abi in REQUIRED_APP_LIBRARIES
    }
    return {
        abi: sorted(required - packaged[abi])
        for abi, required in REQUIRED_APP_LIBRARIES.items()
        if required - packaged[abi]
    }


def utility_identity(data: bytes) -> tuple[str, int]:
    version = UTILITY_VERSION_PATTERN.findall(data)
    code = UTILITY_CODE_PATTERN.findall(data)
    if len(version) != 1 or len(code) != 1:
        raise ArtifactContractError(
            "util_functions.sh must contain exactly one Magisk version and version code"
        )
    try:
        return version[0].decode("ascii"), int(code[0])
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactContractError("invalid util_functions.sh version identity") from exc


def daemon_identity(data: bytes) -> tuple[str, int, str]:
    matches = {
        (match.group(1).decode("ascii"), int(match.group(3)), match.group(2).decode("ascii"))
        for match in DAEMON_VERSION_PATTERN.finditer(data)
    }
    if len(matches) != 1:
        raise ArtifactContractError(
            f"Magisk daemon must contain exactly one build identity, found {sorted(matches)}"
        )
    return matches.pop()


def verify_version_identity(
    utility_data: bytes,
    daemon_binaries: dict[str, bytes],
    expected_mode: str,
) -> dict[str, Any]:
    if expected_mode not in {"D", "R"}:
        raise ArtifactContractError(f"invalid expected Magisk build mode: {expected_mode}")
    utility_version, utility_code = utility_identity(utility_data)
    if "kitsune" not in utility_version:
        raise ArtifactContractError(
            "Magisk version does not contain the lowercase Kitsune identity marker"
        )
    daemon_identities = {
        name: daemon_identity(data) for name, data in daemon_binaries.items()
    }
    unique = set(daemon_identities.values())
    if len(unique) != 1:
        raise ArtifactContractError(
            f"packaged Magisk daemons disagree on build identity: {daemon_identities}"
        )
    daemon_version, daemon_code, daemon_mode = unique.pop()
    if (daemon_version, daemon_code) != (utility_version, utility_code):
        raise ArtifactContractError(
            "manager/native Magisk version mismatch: "
            f"utility={utility_version}/{utility_code}, "
            f"daemon={daemon_version}/{daemon_code}"
        )
    if daemon_mode != expected_mode:
        raise ArtifactContractError(
            f"APK variant expects daemon mode {expected_mode}, found {daemon_mode}"
        )
    return {
        "name": utility_version,
        "code": utility_code,
        "daemon_mode": daemon_mode,
    }


def verify_system_mode_surface(
    entries: dict[str, bytes], expected_mode: str
) -> dict[str, Any]:
    """Verify every APK-driven System Mode entry point has the same build gate."""
    required = (RECOVERY_SCRIPT_PATH, ADDON_SCRIPT_PATH)
    missing = [path for path in required if path not in entries]
    if missing:
        raise ArtifactContractError(f"manager APK is missing installer scripts: {missing}")

    gate_commands = {
        RECOVERY_SCRIPT_PATH: b'"$MODE_BINARY" -c',
        ADDON_SCRIPT_PATH: b'"$mode_binary" -c',
    }
    stale_gate_commands = {
        RECOVERY_SCRIPT_PATH: b'"$MODE_BINARY" -v',
        ADDON_SCRIPT_PATH: b'"$mode_binary" -v',
    }
    for path in required:
        script = entries[path]
        gate = script.find(b":MAGISK:D ")
        mutation = script.find(b"remove_system_su")
        if (
            gate_commands[path] not in script
            or stale_gate_commands[path] in script
            or gate < 0
            or mutation < 0
            or gate >= mutation
        ):
            raise ArtifactContractError(
                f"{path} does not reject release System Mode before mutation"
            )
        remove = script.find(b"rm -f ./manager.sh", gate)
        extract = script.find(b'unzip -oj', remove)
        require = script.find(b"[ -f ./manager.sh ]", extract)
        source = script.find(b". ./manager.sh ||", require)
        if min(remove, extract, require, source) < 0 or not (
            remove < extract < require < source
        ):
            raise ArtifactContractError(
                f"{path} can source a stale or missing System Mode manager"
            )

    addon = entries[ADDON_SCRIPT_PATH]
    if (
        b"system_apk=$MAGISKBIN/magisk.apk" not in addon
        or b'direct_install_system "$MAGISKBIN"' not in addon
        or b"MAGISKBINTMP" in addon
    ):
        raise ArtifactContractError(
            "addon.d does not use the restored System Mode payload consistently"
        )

    manager_present = SYSTEM_MODE_MANAGER_PATH in entries
    if expected_mode == "D":
        if (
            not manager_present
            or b'"$mode_binary" -c' not in entries[SYSTEM_MODE_MANAGER_PATH]
            or b'"$mode_binary" -v' in entries[SYSTEM_MODE_MANAGER_PATH]
            or b":MAGISK:D " not in entries[SYSTEM_MODE_MANAGER_PATH]
        ):
            raise ArtifactContractError(
                "debug APK is missing its guarded System Mode manager"
            )
    elif manager_present:
        raise ArtifactContractError(
            "release APK unexpectedly exposes the debug-only System Mode manager"
        )

    return {
        "recovery_gate": True,
        "addon_gate": True,
        "debug_manager_present": manager_present,
    }


def analyze_apk(apk: Path, apksigner: Path, minimum_alignment: int) -> dict[str, Any]:
    if not apk.is_file():
        raise ArtifactContractError(f"APK does not exist: {apk}")
    native: dict[str, list[int]] = {}
    try:
        with zipfile.ZipFile(apk) as archive:
            archive_names = archive.namelist()
            duplicate_names = sorted(
                name for name, count in Counter(archive_names).items() if count > 1
            )
            if duplicate_names:
                raise ArtifactContractError(
                    f"{apk} contains duplicate ZIP entries: {duplicate_names}"
                )
            names = sorted(
                name for name in archive_names
                if name.startswith("lib/") and name.endswith(".so")
            )
            daemon_binaries: dict[str, bytes] = {}
            native_zip_entries: dict[str, dict[str, Any]] = {}
            for name in names:
                info = archive.getinfo(name)
                data_offset = zip_entry_data_offset(archive, info)
                stored = info.compress_type == zipfile.ZIP_STORED
                if stored and data_offset % minimum_alignment != 0:
                    raise ArtifactContractError(
                        f"{apk}:{name} is stored at ZIP offset {data_offset}, "
                        f"not a {minimum_alignment}-byte boundary"
                    )
                native_zip_entries[name] = {
                    "stored": stored,
                    "data_offset": data_offset,
                    "aligned_when_required": not stored
                        or data_offset % minimum_alignment == 0,
                }
                data = archive.read(info)
                parts = Path(name).parts
                if len(parts) != 3 or parts[1] not in ABI_ELF_IDENTITIES:
                    raise ArtifactContractError(f"{apk}:{name} uses an unexpected ABI path")
                elf_class, machine, alignments = elf_identity_and_load_alignments(data)
                expected_class, expected_machine = ABI_ELF_IDENTITIES[parts[1]]
                if (elf_class, machine) != (expected_class, expected_machine):
                    raise ArtifactContractError(
                        f"{apk}:{name} has ELF class/machine "
                        f"{elf_class}/{machine}, expected "
                        f"{expected_class}/{expected_machine} for {parts[1]}"
                    )
                invalid = [
                    alignment for alignment in alignments
                    if alignment < minimum_alignment or alignment % minimum_alignment != 0
                ]
                if invalid:
                    formatted = ", ".join(hex(value) for value in invalid)
                    raise ArtifactContractError(
                        f"{apk}:{name} has incompatible PT_LOAD alignment: {formatted}"
                    )
                native[name] = alignments
                if Path(name).name in {"libmagisk32.so", "libmagisk64.so"}:
                    daemon_binaries[name] = data

            version_identity = None
            system_mode_surface = None
            if apk.name.startswith("app-"):
                utility_path = "assets/util_functions.sh"
                if utility_path not in archive_names:
                    raise ArtifactContractError(
                        f"manager APK is missing {utility_path}: {apk}"
                    )
                if apk.name.endswith("-debug.apk"):
                    expected_mode = "D"
                elif apk.name.endswith("-release.apk"):
                    expected_mode = "R"
                else:
                    raise ArtifactContractError(
                        f"cannot determine manager APK build variant: {apk}"
                    )
                version_identity = verify_version_identity(
                    archive.read(utility_path), daemon_binaries, expected_mode
                )
                installer_paths = {
                    RECOVERY_SCRIPT_PATH,
                    ADDON_SCRIPT_PATH,
                    SYSTEM_MODE_MANAGER_PATH,
                }
                system_mode_surface = verify_system_mode_surface(
                    {
                        path: archive.read(path)
                        for path in installer_paths
                        if path in archive_names
                    },
                    expected_mode,
                )
    except zipfile.BadZipFile as exc:
        raise ArtifactContractError(f"invalid APK ZIP: {apk}") from exc

    result = {
        "path": str(apk),
        "size": apk.stat().st_size,
        "sha256": file_sha256(apk),
        "certificate_sha256": signer_certificates(apksigner, apk)[0],
        "native_libraries": {
            name: [hex(value) for value in alignments]
            for name, alignments in native.items()
        },
        "native_zip_entries": native_zip_entries,
    }
    if version_identity is not None:
        result["magisk_identity"] = version_identity
        result["system_mode_surface"] = system_mode_surface
    return result


def verify_contract(
    apks: list[Path],
    apksigner: Path,
    minimum_alignment: int,
    rejected_certificates: set[str],
    expected_release_certificate: str | None = None,
) -> dict[str, Any]:
    if minimum_alignment < 1 or minimum_alignment & (minimum_alignment - 1):
        raise ArtifactContractError("minimum alignment must be a positive power of two")
    if not apksigner.is_file():
        raise ArtifactContractError(f"apksigner does not exist: {apksigner}")

    artifacts = [analyze_apk(apk, apksigner, minimum_alignment) for apk in apks]
    for artifact in artifacts:
        certificate = artifact["certificate_sha256"]
        if certificate in rejected_certificates:
            raise ArtifactContractError(
                f"{artifact['path']} uses a forbidden signer certificate: {certificate}"
            )

    variants: dict[str, set[str]] = {"debug": set(), "release": set()}
    for artifact in artifacts:
        name = Path(artifact["path"]).name
        for variant in variants:
            if name.endswith(f"-{variant}.apk"):
                variants[variant].add(artifact["certificate_sha256"])
    for variant, certificates in variants.items():
        if certificates and len(certificates) != 1:
            raise ArtifactContractError(
                f"{variant} APKs do not share one signer certificate: {sorted(certificates)}"
            )
    if variants["debug"] and variants["release"] and variants["debug"] == variants["release"]:
        raise ArtifactContractError("debug and release APKs use the same signer certificate")
    if expected_release_certificate is not None:
        expected_release_certificate = expected_release_certificate.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_release_certificate):
            raise ArtifactContractError("expected release certificate must be 64 hexadecimal digits")
        if variants["release"] != {expected_release_certificate}:
            raise ArtifactContractError(
                "release APK signer does not match the expected production identity: "
                f"expected {expected_release_certificate}, found {sorted(variants['release'])}"
            )

    app_artifacts = [item for item in artifacts if Path(item["path"]).name.startswith("app-")]
    for artifact in app_artifacts:
        if not artifact["native_libraries"]:
            raise ArtifactContractError(f"manager APK contains no native libraries: {artifact['path']}")
        missing = missing_required_libraries(artifact["native_libraries"])
        if missing:
            raise ArtifactContractError(
                f"manager APK is missing required ABI payloads: {artifact['path']}: {missing}"
            )

    return {
        "schema_version": 3,
        "minimum_load_alignment": minimum_alignment,
        "artifacts": artifacts,
        "validation": {"passed": True},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify APK signing separation and 16 KiB ELF/ZIP alignment"
    )
    parser.add_argument("--apk", action="append", type=Path, required=True)
    parser.add_argument("--apksigner", type=Path, required=True)
    parser.add_argument("--minimum-alignment", type=int, default=16_384)
    parser.add_argument("--reject-certificate", action="append", default=[])
    parser.add_argument("--expected-release-certificate")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = verify_contract(
            [path.resolve() for path in args.apk],
            args.apksigner.resolve(),
            args.minimum_alignment,
            {value.lower() for value in args.reject_certificate},
            args.expected_release_certificate,
        )
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
    except (ArtifactContractError, OSError) as exc:
        print(f"artifact contract failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
