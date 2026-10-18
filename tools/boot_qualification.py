#!/usr/bin/env python3
"""Exercise the APK's real MagiskBoot on disposable image fixtures over ADB.

These are parser/patch/signing tests, not evidence that an OEM phone boots.
OpenSSL is an independent AVB1 signature oracle; no production keys are used.
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import uuid
import zipfile
import zlib


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pad(data: bytes, alignment: int) -> bytes:
    return data + bytes(-len(data) % alignment)


def cpio(files: dict[str, bytes]) -> bytes:
    archive = b""
    for index, (name, data) in enumerate([*sorted(files.items()), ("TRAILER!!!", b"")]):
        filename = name.encode() + b"\0"
        fields = [index, 0o100750, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(filename), 0]
        archive += pad(b"070701" + b"".join(f"{n:08x}".encode() for n in fields) + filename, 4)
        archive += pad(data, 4)
    return archive


def dtb() -> bytes:
    # A nonempty FDT v17; MagiskBoot intentionally rejects empty-root false positives.
    model = b"Kitsune boot parser qualification fixture; no hardware identity\0"
    tree = struct.pack(">I", 1) + bytes(4)
    tree += struct.pack(">III", 3, len(model), 0) + pad(model, 4)
    tree += struct.pack(">II", 2, 9)
    strings = pad(b"model\0", 4)
    return struct.pack(">10I", 0xD00DFEED, 56 + len(tree) + len(strings), 56,
                       56 + len(tree), 40, 17, 16, 0, len(strings), len(tree)) + bytes(16) + tree + strings


def boot_image(version: int, kernel: bytes, ramdisk: bytes, *, vendor: bool = False) -> bytes:
    if vendor:
        header = b"VNDRBOOT" + struct.pack("<5I", version, 4096, 0, 0, len(ramdisk))
        header += bytes(2048) + struct.pack("<I", 0) + bytes(16)
        header += struct.pack("<IIQ", 2128 if version == 4 else 2112, 0, 0)
        if version == 4:
            table = struct.pack("<III", len(ramdisk), 0, 1) + b"init_boot".ljust(32, b"\0") + bytes(64)
            config = b"androidboot.test = \"fixture\"\n"
            header += struct.pack("<4I", len(table), 1, len(table), len(config))
            return pad(header, 4096) + pad(ramdisk, 4096) + pad(table, 4096) + pad(config, 4096)
        return pad(header, 4096) + pad(ramdisk, 4096)
    if version <= 2:
        header = b"ANDROID!" + struct.pack("<10I", len(kernel), 0, len(ramdisk), 0,
                                           0, 0, 0, 4096, version, 0) + bytes(1584)
        if version >= 1:
            header += struct.pack("<IQI", 0, 0, 1648 if version == 1 else 1660)
        if version == 2:
            header += struct.pack("<IQ", 0, 0)
    else:
        header = b"ANDROID!" + struct.pack("<9I", len(kernel), len(ramdisk), 0,
                                           1580 if version == 3 else 1584,
                                           0, 0, 0, 0, version) + bytes(1536)
        if version == 4:
            header += bytes(4)
    return pad(header, 4096) + pad(kernel, 4096) + pad(ramdisk, 4096)


def der(tag: int, value: bytes) -> bytes:
    length = len(value)
    encoded = bytes([length]) if length < 128 else (
        bytes([0x80 | ((length.bit_length() + 7) // 8)])
        + length.to_bytes((length.bit_length() + 7) // 8, "big")
    )
    return bytes([tag]) + encoded + value


def der_items(data: bytes) -> list[tuple[int, bytes, bytes]]:
    items = []
    pos = 0
    while pos < len(data):
        start = pos
        tag, size = data[pos:pos + 2]
        pos += 2
        if size & 128:
            count = size & 127
            if not 0 < count <= 4 or pos + count > len(data):
                raise ValueError("invalid fixture DER length")
            size = int.from_bytes(data[pos:pos + count], "big")
            pos += count
        end = pos + size
        if end > len(data):
            raise ValueError("truncated fixture DER")
        items.append((tag, data[pos:end], data[start:end]))
        pos = end
    return items


class Runner:
    def __init__(self, adb: str, serial: str, apk: Path, work: Path):
        self.adb = [adb, "-s", serial]
        self.apk = apk
        self.work = work
        self.guest = "/data/local/tmp/kitsune-boot-" + uuid.uuid4().hex
        self.checks: dict[str, str] = {}

    def run(self, command: list[str], *, ok: tuple[int, ...] = (0,)) -> bytes:
        result = subprocess.run(command, capture_output=True, timeout=90)
        if result.returncode not in ok:
            raise RuntimeError(f"{command[0]} failed ({result.returncode}): "
                               + (result.stdout + result.stderr).decode(errors="replace"))
        return result.stdout

    def shell(self, command: str, *, ok: tuple[int, ...] = (0,)) -> bytes:
        return self.run([*self.adb, "shell", command], ok=ok)

    def boot(self, *args: str, ok: tuple[int, ...] = (0,)) -> bytes:
        return self.shell(f"cd {self.guest} && ./magiskboot " + shlex.join(args), ok=ok)

    def put(self, name: str, data: bytes) -> None:
        file = self.work / name
        file.write_bytes(data)
        self.run([*self.adb, "push", str(file), f"{self.guest}/{name}"])

    def get(self, name: str) -> bytes:
        return self.run([*self.adb, "exec-out", "cat", f"{self.guest}/{name}"])

    def check(self, name: str, condition: bool) -> None:
        if not condition:
            raise RuntimeError(f"fixture failed: {name}")
        self.checks[name] = "passed"
        print(name + ": passed", flush=True)

    def setup(self) -> None:
        abi = self.shell("getprop ro.product.cpu.abi").decode().strip()
        with zipfile.ZipFile(self.apk) as archive:
            self.shell(f"mkdir -m 700 {self.guest}")
            for name in ("magiskboot", "magiskinit", "magisk", "init-ld"):
                self.put(name, archive.read(f"lib/{abi}/lib{name}.so"))
            for name in ("stub.apk", "boot_patch.sh"):
                self.put(name, archive.read(f"assets/{name}"))
        self.shell(f"chmod 755 {self.guest}/magiskboot")

    def signatures(self) -> None:
        image = boot_image(0, b"test-kernel", cpio({"init": b"original-init"}))
        length = len(image).to_bytes((len(image).bit_length() + 7) // 8, "big")
        if length[0] & 128:
            length = b"\0" + length
        attr = der(0x30, der(0x13, b"/boot") + der(2, length))
        algorithms = {
            "prime256v1": bytes.fromhex("300a06082a8648ce3d040302"),
            "secp384r1": bytes.fromhex("300a06082a8648ce3d040303"),
            "secp521r1": bytes.fromhex("300a06082a8648ce3d040304"),
            "rsa": bytes.fromhex("300d06092a864886f70d01010b0500"),
        }
        p256_key = b""
        for curve, hash_name in (("prime256v1", "sha256"), ("secp384r1", "sha384"),
                                  ("secp521r1", "sha512"), ("rsa", "sha256")):
            key, cert = self.work / "key.pem", self.work / "cert.pem"
            options = ["rsa:2048"] if curve == "rsa" else ["ec", "-pkeyopt", f"ec_paramgen_curve:{curve}"]
            cert_hash = "sha384" if hash_name == "sha256" else "sha256"
            self.run(["openssl", "req", "-new", "-x509", "-newkey", *options,
                      "-nodes", f"-{cert_hash}", "-keyout", str(key), "-out", str(cert),
                      "-days", "1", "-subj", "/CN=Kitsune disposable boot fixture"])
            pk8 = self.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", str(key), "-outform", "DER"])
            certificate = self.run(["openssl", "x509", "-in", str(cert), "-outform", "DER"])
            alg = algorithms[curve]
            if curve == "prime256v1":
                p256_key = pk8
            payload = self.work / "authenticated.bin"
            payload.write_bytes(image + attr)
            signature = self.run(["openssl", "dgst", f"-{hash_name}", "-sign", str(key), str(payload)])
            record = der(0x30, der(2, b"\1") + certificate + alg + attr + der(4, signature))
            self.put("reference.img", image + record)
            self.boot("verify", "reference.img")
            self.check(f"{curve}:verify-independent-signature", True)
            self.put("candidate.img", image)
            self.put("key.pk8", pk8)
            self.put("cert.pem", cert.read_bytes())
            self.boot("sign", "candidate.img", "/boot", "cert.pem", "key.pk8")
            signed = self.get("candidate.img")
            fields = der_items(der_items(signed[len(image):])[0][1])
            self.check(f"{curve}:algorithm-describes-image-not-issuer", fields[2][2] == alg)
            payload.write_bytes(image + fields[3][2])
            signature_path = self.work / "signature.bin"
            signature_path.write_bytes(fields[4][1])
            public = self.work / "public.pem"
            public.write_bytes(self.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"]))
            self.run(["openssl", "dgst", f"-{hash_name}", "-verify", str(public),
                      "-signature", str(signature_path), str(payload)])
            self.check(f"{curve}:independent-verification", True)
            self.put("corrupt.img", image[:4096] + bytes([image[4096] ^ 1]) + image[4097:] + record)
            self.boot("verify", "corrupt.img", ok=(1,))
            self.check(f"{curve}:tamper-rejected", True)
            for label, version, algorithm in (("version", 2, alg),
                    ("algorithm", 1, algorithms["secp521r1" if curve == "prime256v1" else "prime256v1"])):
                invalid = der(0x30, der(2, bytes([version])) + certificate + algorithm + attr + der(4, signature))
                self.put("invalid.img", image + invalid)
                self.boot("verify", "invalid.img", ok=(1,))
                self.check(f"{curve}:wrong-{label}-rejected", True)
            if curve == "secp521r1":
                self.put("mismatch.img", image)
                self.put("wrong.pk8", p256_key)
                self.boot("sign", "mismatch.img", "/boot", "cert.pem", "wrong.pk8", ok=(1,))
                self.check("mismatched-certificate:input-untouched", self.get("mismatch.img") == image)
            if curve != "rsa":
                integers = der_items(der_items(signature)[0][1])
                width = {"prime256v1": 32, "secp384r1": 48, "secp521r1": 66}[curve]
                raw = b"".join(int.from_bytes(i[1], "big").to_bytes(width, "big") for i in integers)
                invalid = der(0x30, der(2, b"\1") + certificate + alg + attr + der(4, raw))
                self.put("raw.img", image + invalid)
                self.boot("verify", "raw.img", ok=(1,))
                self.check(f"{curve}:nonstandard-raw-rejected", True)

    def patch_images(self) -> None:
        original_init = b"original-init-fixture\0/system/bin/init\0/init.real\0"
        ramdisk = gzip.compress(cpio({"init": original_init, "init.rc": b"on early-init\n"}), mtime=0)
        vivo = bytes.fromhex("0092CFC2C9CDDDDA00")
        patched_vivo = bytes.fromhex("0092CFC2C9CEC0DB00")
        cases = [(f"boot-v{v}", v, False, b"fixture-kernel") for v in range(5)]
        cases += [("init_boot-v4", 4, False, b""), ("vendor_boot-v3", 3, True, b""),
                  ("vendor_boot-v4", 4, True, b""),
                  ("vivo", 0, False, b"prefix" + vivo + b"suffix"),
                  ("samsung-proca", 0, False, b"prefixproca_config\0suffix")]
        for name, version, vendor, kernel in cases:
            self.boot("cleanup")
            image = boot_image(version, gzip.compress(kernel, mtime=0) if kernel else b"", ramdisk, vendor=vendor)
            self.put("stock.img", image)
            self.shell(f"cd {self.guest} && SOURCEDMODE=1; BOOTMODE=false; "
                       "ui_print() { echo \"$*\"; }; abort() { echo \"$*\" >&2; exit 1; }; "
                       ". ./boot_patch.sh stock.img")
            patched = self.get("new-boot.img")
            self.check(f"{name}:source-preserved", self.get("stock.img") == image)
            self.boot("cleanup")
            self.boot("unpack", "new-boot.img", ok=(3,) if vendor else (0,))
            cpio_path = "vendor_ramdisk/init_boot.cpio" if vendor and version == 4 else "ramdisk.cpio"
            self.boot("cpio", cpio_path, "test", ok=(1,))
            self.boot("cpio", cpio_path, "restore", "extract init restored-init")
            self.check(f"{name}:original-init-restored", self.get("restored-init") == original_init)
            if kernel:
                expected = kernel.replace(vivo, patched_vivo).replace(b"proca_config\0", b"proca_magisk\0")
                self.check(f"{name}:kernel-bytes", self.get("kernel") == expected)
            self.check(f"{name}:patch-produced-image", image != patched)
        self.boot("cleanup")
        kernel = b"compressed-kernel-fixture" * 100
        compressed = gzip.compress(kernel, mtime=0)
        self.put("combined", compressed + dtb())
        self.boot("split", "-n", "combined")
        self.check("split-n:compressed-kernel-preserved", self.get("kernel") == compressed)
        self.check("split-n:dtb-preserved", self.get("kernel_dtb") == dtb())
        self.boot("split", "combined")
        self.check("split:kernel-decompressed", self.get("kernel") == kernel)

    def damaged_compression(self) -> None:
        ramdisk = cpio({"init": b"compression-fixture" * 100})
        formats = {"gzip": gzip.compress(ramdisk, mtime=0), "xz": lzma.compress(ramdisk),
                   "bzip2": bz2.compress(ramdisk)}
        for name, compressed in formats.items():
            damaged = compressed[:-8]
            for part in ("kernel", "ramdisk", "vendor-ramdisk"):
                self.boot("cleanup")
                image = boot_image(4 if part == "vendor-ramdisk" else 0,
                                   damaged if part == "kernel" else b"fixture-kernel",
                                   damaged if part != "kernel" else ramdisk,
                                   vendor=part == "vendor-ramdisk")
                self.put("damaged.img", image)
                self.boot("unpack", "damaged.img", ok=(1,))
                output = {"kernel": "kernel", "ramdisk": "ramdisk.cpio",
                          "vendor-ramdisk": "vendor_ramdisk/init_boot.cpio"}[part]
                self.shell(f"test ! -e {self.guest}/{output}")
                self.check(f"{name}:{part}:failure-removes-partial-output", True)
                self.check(f"{name}:{part}:damaged-input-unchanged", self.get("damaged.img") == image)
            self.boot("cleanup")
            self.put("damaged-combined", damaged + dtb())
            self.boot("split", "damaged-combined", ok=(1,))
            self.shell(f"test ! -e {self.guest}/kernel")
            self.check(f"{name}:split-removes-partial-kernel", True)

        for name, compressed in {"gzip": gzip.compress(b"", mtime=0),
                                 "xz": lzma.compress(b""), "bzip2": bz2.compress(b"")}.items():
            self.put("empty-compressed", compressed)
            self.boot("decompress", "empty-compressed", "empty-result")
            self.check(f"{name}:empty-stream-compatible", self.get("empty-result") == b"")

        self.boot("cleanup")
        self.put("output-error.img", boot_image(0, gzip.compress(b"fixture-kernel", mtime=0), ramdisk))
        self.shell(f"mkdir {self.guest}/kernel && echo retained > {self.guest}/kernel/sentinel")
        try:
            self.boot("unpack", "output-error.img", ok=(1,))
            self.check("unpack:output-open-failure-preserves-directory",
                       self.get("kernel/sentinel") == b"retained\n")
        finally:
            self.shell(f"rm -rf {self.guest}/kernel")

    def vendor_footers(self) -> None:
        self.boot("cleanup")
        image = boot_image(0, b"fixture-kernel", cpio({"init": b"fixture"}))
        signer = b"SignerVer02" + bytes(i % 251 for i in range(512 - len(b"SignerVer02")))
        self.put("samsung.img", image + b"SEANDROIDENFORCE" + signer)
        self.boot("unpack", "samsung.img")
        self.boot("repack", "samsung.img", "samsung-repacked.img")
        result = self.get("samsung-repacked.img")
        kernel_size = struct.unpack_from("<I", result, 8)[0]
        ramdisk_size = struct.unpack_from("<I", result, 16)[0]
        tail = 4096 + kernel_size + (-kernel_size % 4096) + ramdisk_size + (-ramdisk_size % 4096)
        self.check("samsung:SignerVer02-preserved",
                   result[tail:tail + 16 + 512] == b"SEANDROIDENFORCE" + signer)

        # An unsigned AVB2 fixture with one real hash descriptor. Repacking may
        # move vbmeta and grow the AOSP payload, but cannot re-sign that descriptor.
        self.boot("cleanup")
        body = struct.pack(">Q", len(image)) + b"sha256".ljust(32, b"\0")
        body += struct.pack(">4I", 4, 0, 32, 0) + bytes(60) + b"boot" + hashlib.sha256(image).digest()
        descriptor = struct.pack(">QQ", 2, len(body)) + body
        header = bytearray(256)
        struct.pack_into(">4sIIQQI", header, 0, b"AVB0", 1, 0, 0, len(pad(descriptor, 64)), 0)
        struct.pack_into(">QQ", header, 96, 0, len(descriptor))
        vbmeta = bytes(header) + pad(descriptor, 64)
        footer = struct.pack(">4sIIQQQ", b"AVBf", 1, 0, len(image), len(image), len(vbmeta)) + bytes(28)
        partition_size = 128 * 1024
        partition = image + vbmeta + bytes(partition_size - 64 - len(image) - len(vbmeta)) + footer
        self.put("avb2.img", partition)
        self.boot("unpack", "avb2.img")
        self.put("ramdisk.cpio", cpio({"init": b"fixture", "larger": bytes(12000)}))
        self.boot("repack", "avb2.img", "avb2-repacked.img")
        result = self.get("avb2-repacked.img")
        _, _, _, image_size, offset, size = struct.unpack_from(">4sIIQQQ", result, len(result) - 64)
        self.check("avb2:footer-tracks-grown-image", image_size > len(image))
        self.check("avb2:hash-descriptor-preserved", result[offset:offset + size] == vbmeta)
        self.check("avb2:source-preserved", self.get("avb2.img") == partition)

    def zimage_payloads(self) -> None:
        payload = b"ARM32 compressed kernel fixture" * 100
        compressed = gzip.compress(payload, mtime=0)
        for start in (256, 257):
            self.boot("cleanup")
            end = start + len(compressed)
            head = bytearray(start)
            struct.pack_into("<III", head, 0x24, 0x016F2818, 0, end + 64)
            struct.pack_into("<II", head, 0x34, 0x45454545, 128)
            struct.pack_into("<III", head, 128, 3, 0x5A534C4B, end - 4)
            struct.pack_into("<I", head, 192, end - 192 - 4)
            # A known but non-compressed magic must not mask the actual payload.
            head[64:72] = b"ANDROID!"
            kernel = bytes(head) + compressed + bytes(64)
            image = boot_image(0, kernel, cpio({"init": b"fixture"}))
            self.put("zimage.img", image)
            self.boot("unpack", "zimage.img")
            self.check(f"zimage:{start}:finds-compressed-payload", self.get("kernel") == payload)
            resized = payload + b"resized kernel fixture"
            self.put("kernel", resized)
            self.boot("repack", "zimage.img", "zimage-repacked.img")
            result = self.get("zimage-repacked.img")
            size = struct.unpack_from("<I", result, 8)[0]
            kernel = result[4096:4096 + size]
            decoder = zlib.decompressobj(wbits=31)
            self.check(f"zimage:{start}:independent-gzip-round-trip",
                       decoder.decompress(kernel[start:]) == resized and decoder.eof)
            payload_end = len(kernel) - len(decoder.unused_data)
            self.check(f"zimage:{start}:exact-size-pointers",
                       struct.unpack_from("<I", kernel, 136)[0] == payload_end - 4
                       and struct.unpack_from("<I", kernel, 192)[0] == payload_end - 192 - 4)
            self.boot("cleanup")
            self.boot("unpack", "zimage-repacked.img")
            self.check(f"zimage:{start}:resized-round-trip", self.get("kernel") == resized)
            self.check(f"zimage:{start}:source-preserved", self.get("zimage.img") == image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--apk", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kitsune-boot-fixtures-") as temp:
        runner = Runner(args.adb, args.serial, args.apk, Path(temp))
        try:
            runner.setup()
            runner.signatures()
            runner.patch_images()
            runner.damaged_compression()
            runner.vendor_footers()
            runner.zimage_payloads()
            report = {"schema": 1, "kind": "synthetic-boot-fixtures", "apk_sha256": digest(args.apk.read_bytes()),
                      "fingerprint": runner.shell("getprop ro.build.fingerprint").decode().strip(),
                      "checks": runner.checks, "physical_boot_qualified": False}
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(f"Passed {len(runner.checks)} boot fixture checks")
        finally:
            runner.shell(f"rm -rf {runner.guest}")


if __name__ == "__main__":
    main()
