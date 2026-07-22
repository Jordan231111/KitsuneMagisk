from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import random
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zipfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CRASH_MARKERS = (
    "AddressSanitizer",
    "UndefinedBehaviorSanitizer",
    "ubsan:",
    "runtime error:",
    "Segmentation fault",
    "SIGSEGV",
    "stack-buffer-overflow",
    "heap-buffer-overflow",
)
ABI_NAMES = {"arm64-v8a", "armeabi-v7a", "x86", "x86_64"}


class CorpusFailure(RuntimeError):
    pass


class Adb:
    def __init__(self, executable: str, serial: str, timeout: int):
        self.executable = executable
        self.serial = serial
        self.timeout = timeout

    @property
    def prefix(self) -> list[str]:
        return [self.executable, "-s", self.serial]

    def run(self, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*self.prefix, *args],
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CorpusFailure(f"ADB command timed out: {' '.join(args)}") from exc

    def require(self, *args: str) -> str:
        proc = self.run(*args)
        if proc.returncode != 0:
            raise CorpusFailure(
                f"ADB {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    def shell(self, command: str, *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        # adb concatenates shell arguments into one remote command line. Keep
        # the complete script as sh -c's single argument after that join.
        return self.run("shell", "sh", "-c", shlex.quote(command), timeout=timeout)


def _safe_remote_root(value: str) -> str:
    path = PurePosixPath(value)
    if path.parent != PurePosixPath("/data/local/tmp"):
        raise ValueError("device corpus root must be directly below /data/local/tmp")
    if not path.name.startswith("kitsune-security-"):
        raise ValueError("device corpus root must use the kitsune-security- prefix")
    if not path.name.replace("-", "").isalnum():
        raise ValueError("device corpus root contains unsafe characters")
    return str(path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_minimal_boot_image() -> bytes:
    """Build a deterministic Android boot header v0 image for sign/verify tests."""
    page_size = 2048
    kernel = b"KITSUNE-KERNEL\n"
    # An empty newc archive terminator is sufficient for parser/repack coverage.
    cpio = (
        b"070701"
        + b"00000000" * 11
        + b"0000000B"
        + b"00000000"
        + b"TRAILER!!!\0"
    )
    cpio += b"\0" * ((4 - len(cpio) % 4) % 4)
    ramdisk = gzip.compress(cpio, compresslevel=9, mtime=0)

    header = bytearray(1632)
    header[:8] = b"ANDROID!"
    struct.pack_into("<I", header, 8, len(kernel))
    struct.pack_into("<I", header, 16, len(ramdisk))
    struct.pack_into("<I", header, 36, page_size)
    struct.pack_into("<I", header, 40, 0)
    header[48:48 + len(b"kitsune-lab")] = b"kitsune-lab"
    header[64:64 + len(b"console=null")] = b"console=null"

    def padded(value: bytes) -> bytes:
        return value + b"\0" * ((page_size - len(value) % page_size) % page_size)

    return padded(bytes(header)) + padded(kernel) + padded(ramdisk)


def make_dtb_tail_boot_image() -> bytes:
    """Place a DTB magic at the exact logical end of a bounded kernel."""
    image = bytearray(make_minimal_boot_image())
    page_size = struct.unpack_from("<I", image, 36)[0]
    struct.pack_into("<I", image, 8, 4)
    image[page_size : page_size + 4] = b"\xd0\x0d\xfe\xed"
    return bytes(image)


def seed_corpora() -> dict[str, list[bytes]]:
    return {
        "boot": [
            b"",
            b"ANDROID!",
            b"ANDROID!" + b"\0" * 31,
            b"\xff" * 256,
            b"\x1f\x8b\x08\x00truncated",
            b"070701" + b"0" * 40,
            b"\xd0\x0d\xfe\xed" + b"\0" * 60,
            make_dtb_tail_boot_image(),
            make_minimal_boot_image(),
        ],
        "policy": [
            b"",
            b"\0" * 16,
            b"SE Linux\0" + b"\xff" * 64,
            b"(allow init kernel (process (transition)))",
            b"allow su * * *\n" * 16,
            bytes(range(256)),
        ],
    }


def mutate(seed: bytes, count: int, *, random_seed: int) -> list[bytes]:
    randomizer = random.Random(random_seed)
    result: list[bytes] = []
    for index in range(count):
        data = bytearray(seed)
        operation = index % 5
        if operation == 0 and data:
            data[randomizer.randrange(len(data))] ^= 1 << randomizer.randrange(8)
        elif operation == 1 and data:
            del data[randomizer.randrange(len(data)) : randomizer.randrange(len(data)) + 1]
        elif operation == 2:
            offset = randomizer.randrange(len(data) + 1)
            data[offset:offset] = os.urandom(0) + bytes([randomizer.randrange(256)])
        elif operation == 3 and data:
            data = data[: randomizer.randrange(len(data) + 1)]
        else:
            data.extend(bytes(randomizer.randrange(256) for _ in range(randomizer.randrange(0, 33))))
        result.append(bytes(data[: 2 * 1024 * 1024]))
    return result


def _extract_binaries(apk: Path, abi: str, destination: Path) -> dict[str, Path]:
    if abi not in ABI_NAMES:
        raise CorpusFailure(f"unsupported device ABI: {abi}")
    result = {}
    with zipfile.ZipFile(apk) as archive:
        for name in ("magiskboot", "magiskpolicy"):
            member = f"lib/{abi}/lib{name}.so"
            try:
                data = archive.read(member)
            except KeyError as exc:
                raise CorpusFailure(f"{apk} does not contain {member}") from exc
            target = destination / name
            target.write_bytes(data)
            target.chmod(0o755)
            result[name] = target
    return result


def _binary_paths(binary_dir: Path, abi: str) -> dict[str, Path]:
    if abi not in ABI_NAMES:
        raise CorpusFailure(f"unsupported device ABI: {abi}")
    candidates = [binary_dir / abi, binary_dir]
    for candidate in candidates:
        result = {name: candidate / name for name in ("magiskboot", "magiskpolicy")}
        if all(path.is_file() for path in result.values()):
            return result
    raise CorpusFailure(f"cannot find magiskboot and magiskpolicy below {binary_dir}")


def _command_result(
    adb: Adb,
    command: str,
    *,
    case_id: str,
    require_success: bool = False,
) -> dict[str, Any]:
    proc = adb.shell(command)
    combined = (proc.stdout + "\n" + proc.stderr)[-32_768:]
    markers = [marker for marker in CRASH_MARKERS if marker.lower() in combined.lower()]
    signal_exit = proc.returncode in {132, 133, 134, 135, 136, 137, 138, 139}
    if markers or signal_exit:
        raise CorpusFailure(
            f"{case_id} crashed (exit {proc.returncode}, markers={markers}): {combined[-2000:]}"
        )
    if require_success and proc.returncode != 0:
        raise CorpusFailure(
            f"{case_id} failed (exit {proc.returncode}): {combined[-2000:]}"
        )
    return {
        "id": case_id,
        "command": command,
        "exit_code": proc.returncode,
        "output_sha256": _sha256(combined.encode("utf-8", errors="replace")),
        "output_tail": combined[-1000:],
        "crash_markers": markers,
    }


def _policy_source(adb: Adb) -> str | None:
    candidates = (
        "/vendor/etc/selinux/precompiled_sepolicy",
        "/odm/etc/selinux/precompiled_sepolicy",
        "/sepolicy",
        "/sys/fs/selinux/policy",
    )
    for path in candidates:
        result = adb.shell(f"test -r {shlex.quote(path)}")
        if result.returncode == 0:
            return path
    return None


def _device_page_size(adb: Adb) -> int:
    def valid(value: int) -> bool:
        return 0 < value <= 1024 * 1024 and value & (value - 1) == 0

    getconf = adb.run("shell", "getconf", "PAGESIZE")
    try:
        value = int(getconf.stdout.strip())
    except ValueError:
        value = 0
    if getconf.returncode == 0 and valid(value):
        return value

    # Android 6's shell has no getconf applet. The kernel still reports the
    # actual mapping granule in smaps, so use that rather than assuming 4 KiB.
    smaps = adb.run("shell", "cat", "/proc/self/smaps")
    if smaps.returncode == 0:
        match = re.search(
            r"^(?:Kernel|MMU)PageSize:\s*([0-9]+)\s*kB\s*$",
            smaps.stdout,
            flags=re.MULTILINE,
        )
        if match:
            value = int(match.group(1)) * 1024
            if valid(value):
                return value
    raise CorpusFailure("device page size is unavailable or invalid")


def run_device_corpus(
    adb: Adb,
    binaries: dict[str, Path],
    *,
    iterations: int,
    remote_root: str,
) -> dict[str, Any]:
    remote_root = _safe_remote_root(remote_root)
    adb.require("wait-for-device")
    abi = adb.require("shell", "getprop", "ro.product.cpu.abi").strip()
    page_size = _device_page_size(adb)
    fingerprint = adb.require("shell", "getprop", "ro.build.fingerprint").strip()
    policy_source = _policy_source(adb)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="kitsune-device-corpus-") as temp:
        local = Path(temp)
        corpora = seed_corpora()
        try:
            adb.require("shell", "mkdir", "-p", remote_root)
            for name, path in binaries.items():
                adb.require("push", str(path), f"{remote_root}/{name}")
            adb.require("shell", "chmod", "0755", f"{remote_root}/magiskboot", f"{remote_root}/magiskpolicy")

            for surface, seeds in corpora.items():
                cases: list[bytes] = []
                for seed_index, seed in enumerate(seeds):
                    cases.append(seed)
                    cases.extend(
                        mutate(
                            seed,
                            iterations,
                            random_seed=0x4B17 + seed_index * 257 + (0 if surface == "boot" else 1),
                        )
                    )
                for index, data in enumerate(cases):
                    case_id = f"{surface}-{index:04d}-{_sha256(data)[:12]}"
                    case_path = local / case_id
                    case_path.write_bytes(data)
                    remote_case = f"{remote_root}/{case_id}"
                    adb.require("push", str(case_path), remote_case)
                    if surface == "boot":
                        work = f"{remote_root}/work-{index:04d}"
                        command = (
                            f"mkdir -p {shlex.quote(work)} && cd {shlex.quote(work)} && "
                            f"{shlex.quote(remote_root + '/magiskboot')} unpack -n "
                            f"{shlex.quote(remote_case)}"
                        )
                    else:
                        output = f"{remote_root}/policy-{index:04d}.out"
                        command = (
                            f"{shlex.quote(remote_root + '/magiskpolicy')} --load "
                            f"{shlex.quote(remote_case)} --save {shlex.quote(output)}"
                        )
                    results.append(_command_result(adb, command, case_id=case_id))

            minimal = local / "minimal-boot.img"
            minimal.write_bytes(make_minimal_boot_image())
            adb.require("push", str(minimal), f"{remote_root}/minimal-boot.img")
            boot_work = f"{remote_root}/boot-roundtrip"
            boot_command = " && ".join(
                [
                    f"mkdir -p {shlex.quote(boot_work)}",
                    f"cp {shlex.quote(remote_root + '/minimal-boot.img')} {shlex.quote(boot_work + '/boot.img')}",
                    f"cd {shlex.quote(boot_work)}",
                    f"{shlex.quote(remote_root + '/magiskboot')} unpack -n boot.img",
                    f"{shlex.quote(remote_root + '/magiskboot')} repack -n boot.img repacked.img",
                    f"{shlex.quote(remote_root + '/magiskboot')} sign repacked.img",
                    f"{shlex.quote(remote_root + '/magiskboot')} verify repacked.img",
                ]
            )
            results.append(
                _command_result(
                    adb,
                    boot_command,
                    case_id="boot-roundtrip-sign-verify",
                    require_success=True,
                )
            )

            policy_roundtrip: dict[str, Any]
            if policy_source:
                policy_work = f"{remote_root}/policy-roundtrip"
                policy_command = " && ".join(
                    [
                        f"mkdir -p {shlex.quote(policy_work)}",
                        (
                            f"{shlex.quote(remote_root + '/magiskpolicy')} --load "
                            f"{shlex.quote(policy_source)} --save {shlex.quote(policy_work + '/one')}"
                        ),
                        (
                            f"{shlex.quote(remote_root + '/magiskpolicy')} --load "
                            f"{shlex.quote(policy_work + '/one')} --save {shlex.quote(policy_work + '/two')}"
                        ),
                        (
                            f"{shlex.quote(remote_root + '/magiskpolicy')} --load "
                            f"{shlex.quote(policy_work + '/one')} --print-rules | "
                            f"LC_ALL=C sort -u > {shlex.quote(policy_work + '/one.rules')}"
                        ),
                        (
                            f"{shlex.quote(remote_root + '/magiskpolicy')} --load "
                            f"{shlex.quote(policy_work + '/two')} --print-rules | "
                            f"LC_ALL=C sort -u > {shlex.quote(policy_work + '/two.rules')}"
                        ),
                        (
                            f"cmp {shlex.quote(policy_work + '/one.rules')} "
                            f"{shlex.quote(policy_work + '/two.rules')}"
                        ),
                    ]
                )
                policy_roundtrip = _command_result(
                    adb,
                    policy_command,
                    case_id="policy-load-save-reload",
                    require_success=True,
                )
                results.append(policy_roundtrip)
            else:
                policy_roundtrip = {"status": "skipped", "reason": "no-readable-policy-source"}
        finally:
            # remote_root is validated above and unique to this invocation.
            cleanup = adb.shell(f"rm -rf -- {shlex.quote(remote_root)}")
            verify = adb.shell(f"test ! -e {shlex.quote(remote_root)}")
            if cleanup.returncode != 0 or verify.returncode != 0:
                detail = (cleanup.stdout + cleanup.stderr + verify.stdout + verify.stderr).strip()
                raise CorpusFailure(
                    f"device corpus sandbox cleanup failed for {remote_root}: {detail}"
                )

    return {
        "schema_version": 1,
        "target": {
            "serial": adb.serial,
            "abi": abi,
            "page_size": page_size,
            "fingerprint_sha256": _sha256(fingerprint.encode()),
        },
        "configuration": {
            "mutations_per_seed": iterations,
            "remote_root": remote_root,
            "policy_source": policy_source,
        },
        "summary": {
            "case_count": len(results),
            "crash_count": 0,
            "boot_sign_verify": "passed",
            "policy_roundtrip": "passed" if policy_source else "skipped",
        },
        "cases": results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run boot/policy parser corpora on Android")
    parser.add_argument("--adb", default=shutil.which("adb") or "adb")
    parser.add_argument("--serial", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--apk", type=Path)
    source.add_argument("--binary-dir", type=Path)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--remote-root")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.iterations < 0 or args.iterations > 256:
        print("iterations must be from 0 through 256", file=sys.stderr)
        return 2
    remote_root = args.remote_root or f"/data/local/tmp/kitsune-security-{uuid.uuid4().hex}"
    try:
        adb = Adb(args.adb, args.serial, args.timeout)
        abi = adb.require("shell", "getprop", "ro.product.cpu.abi").strip()
        with tempfile.TemporaryDirectory(prefix="kitsune-binaries-") as temp:
            if args.apk:
                binaries = _extract_binaries(args.apk, abi, Path(temp))
            else:
                binaries = _binary_paths(args.binary_dir, abi)
            report = run_device_corpus(
                adb,
                binaries,
                iterations=args.iterations,
                remote_root=remote_root,
            )
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        else:
            print(payload, end="")
    except (OSError, ValueError, CorpusFailure, zipfile.BadZipFile) as exc:
        print(f"device corpus failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
