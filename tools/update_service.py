#!/usr/bin/env python3
"""Prepare and verify signed, project-owned update metadata without publishing or rebuilding."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    from tools import build_info, next_system_baseline as baseline, release
except ImportError:
    import build_info
    import next_system_baseline as baseline
    import release

ROOT = Path(__file__).resolve().parents[1]
MAX_METADATA = 131072
MAX_VALIDITY = 30 * 86400
CHANNELS = {"stable", "beta", "canary"}
FIELDS = {
    "schema", "channel", "build_channel", "sequence", "issued_at", "expires_at", "application_id", "product_version",
    "app_version_code", "version_name", "source_commit", "source_digest", "source_dirty", "upstream_version",
    "upstream_commit", "build_id", "protocol_version", "variant", "certificate_sha256", "manager", "stub",
    "release_notes", "tag", "min_sdk",
}


def strict_json(data: bytes) -> dict:
    if len(data) > MAX_METADATA:
        raise ValueError("Metadata exceeds its size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate metadata field")
            result[key] = value
        return result
    result = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("Metadata must be an object")
    return result


def validate_payload(data: dict, trust: dict, *, channel: str, now: int, minimum_code: int = 31001) -> None:
    if set(data) != FIELDS or type(data["schema"]) is not int or data["schema"] != 1:
        raise ValueError("Unsupported metadata schema")
    if channel not in CHANNELS or data["channel"] != channel:
        raise ValueError("Wrong update channel")
    for name in ("sequence", "issued_at", "expires_at", "app_version_code", "protocol_version", "min_sdk"):
        if type(data[name]) is not int or not 0 < data[name] < 2**63:
            raise ValueError("Invalid metadata integer: " + name)
    if not minimum_code <= data["app_version_code"] <= 2100000000 or data["protocol_version"] > 2**31 - 1:
        raise ValueError("Invalid or downgraded app/protocol version")
    if data["min_sdk"] > 10000:
        raise ValueError("Invalid minimum Android version")
    if data["issued_at"] > now + 300 or data["expires_at"] <= now or not 0 < data["expires_at"] - data["issued_at"] <= MAX_VALIDITY:
        raise ValueError("Expired or invalid metadata validity period")
    if data["application_id"] != trust["application_id"] or data["certificate_sha256"] != trust["certificate_sha256"]:
        raise ValueError("Wrong package or certificate")
    if data["variant"] != "release" or type(data["source_dirty"]) is not bool:
        raise ValueError("Invalid release identity")
    import re
    for name, length in [("source_commit", 40), ("upstream_commit", 40), ("source_digest", 64), ("build_id", 64), ("certificate_sha256", 64)]:
        if not isinstance(data[name], str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, data[name]):
            raise ValueError("Invalid metadata digest: " + name)
    for name in ("product_version", "upstream_version", "application_id", "tag", "version_name", "build_channel"):
        if not isinstance(data[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", data[name]):
            raise ValueError("Invalid metadata token: " + name)
    if not data["tag"].startswith("kitsune-") or not isinstance(data["version_name"], str) or not 0 < len(data["version_name"]) <= 128:
        raise ValueError("Invalid release name")
    if not isinstance(data["release_notes"], str) or not 0 < len(data["release_notes"]) <= 32768:
        raise ValueError("Missing or oversized release notes")
    for kind in ("manager", "stub"):
        asset = data[kind]
        if not isinstance(asset, dict) or set(asset) != {"url", "size", "sha256"}:
            raise ValueError("Invalid artifact descriptor")
        maximum = (8 if kind == "stub" else 200) * 1024 * 1024
        if type(asset["size"]) is not int or not 0 < asset["size"] <= maximum:
            raise ValueError("Invalid APK length")
        if not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValueError("Invalid APK digest")
        prefix = f'https://github.com/{trust["repository"]}/releases/download/{data["tag"]}/'
        if not isinstance(asset["url"], str) or not asset["url"].startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9._-]+\.apk", asset["url"][len(prefix):]):
            raise ValueError("Artifact URL is outside the project release")


def signer(mode: str, identity: Path, payload: bytes, signature: bytes | None = None) -> bytes:
    with tempfile.TemporaryDirectory(prefix="kitsune-metadata-") as directory:
        temp = Path(directory)
        (temp / "payload.json").write_bytes(payload)
        if signature is not None:
            (temp / "signature").write_bytes(signature)
        # Source-file mode uses the configured JDK and does not store secret key material.
        subprocess.run(["java", str(ROOT / "tools/UpdateSigner.java"), mode, str(identity),
                        str(temp / "payload.json"), str(temp / "signature")], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return (temp / "signature").read_bytes()


def envelope(payload: dict, config: Path, trust: dict) -> bytes:
    raw = build_info.encoded(payload).encode()
    return raw_envelope(raw, config, trust)


def raw_envelope(raw: bytes, config: Path, trust: dict) -> bytes:
    return (build_info.encoded({"schema": 1, "key_id": trust["certificate_sha256"],
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(signer("sign", config, raw)).decode()}) + "\n").encode()


def fixtures(args, trust: dict) -> None:
    """Generate signed fault fixtures in ignored build output, never in a public feed."""
    import shutil
    import secrets
    destination = args.output.resolve()
    if not destination.is_relative_to(ROOT / "out"):
        raise ValueError("QA fixtures must stay in ignored out/ build output")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.apk) as archive:
        info = json.loads(archive.read("assets/build-info.json"))
        cert = archive.read("assets/manager-cert.der")
    trust = dict(trust, certificate=base64.b64encode(cert).decode(), certificate_sha256=hashlib.sha256(cert).hexdigest())
    (destination / "trust.json").write_text(json.dumps(trust))
    shutil.copyfile(args.apk, destination / "manager.apk")
    shutil.copyfile(args.test_apk, destination / "wrong-package.apk")
    with tempfile.TemporaryDirectory(prefix="kitsune-wrong-signer-") as temporary:
        temp = Path(temporary)
        with zipfile.ZipFile(args.apk) as source, zipfile.ZipFile(temp / "unsigned.apk", "w") as output:
            for name in source.namelist():
                if name in {"AndroidManifest.xml", "resources.arsc", "assets/build-info.json"} or name.startswith("classes") and name.endswith(".dex"):
                    output.writestr(name, source.read(name))
        env = dict(os.environ, KITSUNE_TEST_PASS=secrets.token_urlsafe(24))
        subprocess.run(["keytool", "-genkeypair", "-noprompt", "-keystore", str(temp / "wrong.p12"),
                        "-storepass:env", "KITSUNE_TEST_PASS", "-keypass:env", "KITSUNE_TEST_PASS",
                        "-alias", "test", "-keyalg", "RSA", "-keysize", "3072", "-validity", "2", "-dname", "CN=Disposable negative test"],
                       env=env, check=True, capture_output=True)
        subprocess.run([str(baseline.find_build_tool("apksigner", None)), "sign", "--ks", str(temp / "wrong.p12"),
                        "--ks-pass", "env:KITSUNE_TEST_PASS", "--out", str(destination / "wrong-signer.apk"), str(temp / "unsigned.apk")],
                       env=env, check=True, capture_output=True)
    now = int(time.time())
    valid = make_payload(args.apk, args.stub, info, trust["certificate_sha256"], trust,
                          "beta", 2, now, 1, "Local verifier fixture. This is not a published release.")
    cases = {"valid": valid}
    for name, modifications in {
        "replay": {"sequence": 1}, "equivocation": {"release_notes": "Conflicting signed metadata"},
        "downgrade": {"channel": "stable", "sequence": 3, "app_version_code": info["app_version_code"] - 1},
        "expired": {"issued_at": now - 86400, "expires_at": now - 1},
        "future": {"issued_at": now + 3600, "expires_at": now + 7200},
        "wrong-package-name": {"application_id": "invalid.test.package"},
        "wrong-certificate": {"certificate_sha256": "0" * 64},
        "incompatible-android": {"min_sdk": 10000},
        "wrong-min-sdk": {"min_sdk": valid["min_sdk"] - 1},
    }.items():
        cases[name] = dict(valid, **modifications)
    for kind in ("wrong-package", "wrong-signer"):
        data = copy.deepcopy(valid)
        file = destination / f"{kind}.apk"
        data["manager"] = {"url": valid["manager"]["url"], "size": file.stat().st_size, "sha256": baseline.sha256(file)}
        cases[kind] = data
    data = copy.deepcopy(valid)
    data["manager"]["sha256"] = "0" * 64
    cases["wrong-digest"] = data
    data = copy.deepcopy(valid)
    data["manager"]["size"] += 1
    cases["wrong-length"] = data
    for name, data in cases.items():
        (destination / f"{name}.json").write_bytes(envelope(data, args.config, trust))
    duplicate = build_info.encoded(valid)[:-1] + ',"sequence":2}'
    (destination / "duplicate.json").write_bytes(raw_envelope(duplicate.encode(), args.config, trust))
    print(f"Generated {len(cases) + 1} signed QA cases in {destination}; no private keys retained")


def verify_envelope(raw: bytes, trust: dict, *, channel: str, now: int, historical: bool = False) -> dict:
    outer = strict_json(raw)
    if set(outer) != {"schema", "key_id", "payload", "signature"} or type(outer["schema"]) is not int or outer["schema"] != 1 or outer["key_id"] != trust["certificate_sha256"]:
        raise ValueError("Unknown metadata signer or envelope")
    payload = base64.b64decode(outer["payload"], validate=True)
    signature = base64.b64decode(outer["signature"], validate=True)
    if base64.b64encode(payload).decode() != outer["payload"] or base64.b64encode(signature).decode() != outer["signature"]:
        raise ValueError("Noncanonical metadata base64")
    cert = base64.b64decode(trust["certificate"], validate=True)
    if hashlib.sha256(cert).hexdigest() != trust["certificate_sha256"]:
        raise ValueError("Invalid trust certificate")
    with tempfile.TemporaryDirectory(prefix="kitsune-public-cert-") as directory:
        certificate = Path(directory) / "certificate.der"
        certificate.write_bytes(cert)
        signer("verify", certificate, payload, signature)
    data = strict_json(payload)
    # A publisher may refresh an expired channel, but must retain its signed sequence floor.
    validate_payload(data, trust, channel=channel, now=data.get("issued_at", now) if historical else now)
    return data


class Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 3
    def redirect_request(self, request, fp, code, message, headers, newurl):
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.username or new.password or new.fragment or new.port not in (None, 443):
            raise ValueError("Unsafe update redirect")
        if new.hostname != old.hostname and not (old.hostname == "github.com" and new.hostname == "release-assets.githubusercontent.com"):
            raise ValueError("Unexpected update redirect origin")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch(url: str, maximum: int) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
        raise ValueError("Updates require HTTPS")
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.build_opener(Redirects()).open(request, timeout=30) as response:
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise ValueError("Unexpected transfer encoding")
        result = response.read(maximum + 1)
    if len(result) > maximum:
        raise ValueError("Downloaded update exceeds its size limit")
    return result


def make_payload(apk: Path, stub: Path, info: dict, certificate: str, trust: dict,
                 channel: str, sequence: int, now: int, days: int, notes: str) -> dict:
    minimum_sdks = []
    for path in (apk, stub):
        badging = baseline.run((baseline.find_build_tool("aapt", None), "dump", "badging", path))
        sdk = baseline.SDK_PATTERN.search(badging)
        if sdk is None or not sdk.group(1).isdigit():
            raise ValueError("APK is missing a numeric minimum Android version")
        minimum_sdks.append(int(sdk.group(1)))
    if minimum_sdks[0] != minimum_sdks[1]:
        raise ValueError("Manager and stub require different Android versions")
    fields = {key: info[key] for key in ("application_id", "product_version", "app_version_code", "version_name",
              "source_commit", "source_digest", "source_dirty", "upstream_version", "upstream_commit", "build_id", "protocol_version", "variant")}
    fields.update(schema=1, channel=channel, sequence=sequence, issued_at=now,
                  build_channel=info["channel"], min_sdk=minimum_sdks[0],
                  expires_at=now + days * 86400, certificate_sha256=certificate,
                  release_notes=notes, tag=info["release_tag"])
    for name, path in (("manager", apk), ("stub", stub)):
        fields[name] = {"url": f'https://github.com/{trust["repository"]}/releases/download/{info["release_tag"]}/{path.name}',
                        "size": path.stat().st_size, "sha256": baseline.sha256(path)}
    validate_payload(fields, trust, channel=channel, now=now)
    return fields


def prepare(args, trust: dict) -> None:
    config = args.config.resolve(strict=True)
    if config.is_relative_to(ROOT) or (os.name != "nt" and config.stat().st_mode & 0o077):
        raise ValueError("Signing configuration must be private and outside the checkout")
    info = build_info.variant_info(build_info.identity(), "release")
    if info["source_dirty"]:
        raise ValueError("Production metadata requires the exact clean, committed source")
    receipt = strict_json(args.qualification.read_bytes())
    release.verify_receipt(receipt, build_info.identity(), args.apk, args.stub, args.channel)
    if args.channel == "stable":
        policy = strict_json((ROOT / "docs/release-policy.json").read_bytes())
        ledger = release.port_ledger.audit(ROOT, release.port_ledger.read_json(ROOT / "docs/port-ledger.json"), release=True)
        if policy["production_blockers"] or not ledger["passed"]:
            raise ValueError("Stable promotion still has unresolved release gates")
    manifest = baseline.read_manifest(release.MANIFEST)
    baseline.verify_source(ROOT, manifest)
    aapt = baseline.find_build_tool("aapt", None)
    apksigner = baseline.find_build_tool("apksigner", None)
    record = baseline.inspect_apk(args.apk, "release", aapt, apksigner, manifest, info["version_name"], info)
    baseline.inspect_stub_apk(args.stub, args.apk, record, aapt, apksigner, trust["application_id"])
    if record["certificate_sha256"] != trust["certificate_sha256"]:
        raise ValueError("Candidate is not signed by the project production identity")
    now = int(time.time())
    try:
        previous = verify_envelope(fetch(trust["metadata_base"] + args.channel + ".json", MAX_METADATA), trust,
                                   channel=args.channel, now=now, historical=True)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        previous = None
    if previous and (args.sequence <= previous["sequence"] or info["app_version_code"] < previous["app_version_code"]):
        raise ValueError("Publication would replay or downgrade the current channel")
    if previous and info["app_version_code"] == previous["app_version_code"] and record["sha256"] != previous["manager"]["sha256"]:
        raise ValueError("Different candidate bytes require a new Android upgrade code")
    payload = make_payload(args.apk, args.stub, info, record["certificate_sha256"], trust,
                           args.channel, args.sequence, now, args.days, args.notes.read_text())
    signed = envelope(payload, config, trust)
    verify_envelope(signed, trust, channel=args.channel, now=now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    build_info.write_if_changed(args.output, signed.decode())
    print(f"Prepared {args.output}; tag {info['release_tag']}; no files were published")


def verify_release_assets(release: dict, metadata: dict, trust: dict,
                          output: Path, debug_sha256: str | None = None) -> dict:
    expected_apks = {urllib.parse.urlsplit(metadata[kind]["url"]).path.rsplit("/", 1)[-1] for kind in ("manager", "stub")}
    if debug_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", debug_sha256) is None or release.get("prerelease") is not True:
            raise ValueError("Debug APK verification requires a prerelease and the tested APK's SHA-256")
        expected_apks.add("app-debug.apk")
    published_apks = {asset["name"] for asset in release.get("assets", []) if asset["name"].endswith(".apk")}
    if published_apks != expected_apks:
        raise ValueError("Release contains missing or unapproved APKs; explicitly pin any debug APK and never distribute instrumentation APKs")
    if debug_sha256 is None:
        return {}
    # The debug side download is explicitly pinned to locally tested bytes. It
    # never becomes a production-signed manager/stub update feed candidate.
    url = f'https://github.com/{trust["repository"]}/releases/download/{metadata["tag"]}/app-debug.apk'
    data = fetch(url, 200 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != debug_sha256:
        raise ValueError("Published debug APK differs from the tested bytes")
    output.mkdir(parents=True, exist_ok=True)
    (output / "published-debug.apk").write_bytes(data)
    return {"debug": {"sha256": debug_sha256, "size": len(data), "update_feed": False}}


def verify_published(args, trust: dict) -> None:
    metadata = verify_envelope(fetch(args.url, MAX_METADATA), trust, channel=args.channel, now=int(time.time()))
    api = f'https://api.github.com/repos/{trust["repository"]}'
    release = strict_json(fetch(f'{api}/releases/tags/{metadata["tag"]}', MAX_METADATA))
    if release.get("immutable") is not True or release.get("draft") is not False:
        raise ValueError("Feed must point to a published immutable GitHub release")
    ref = strict_json(fetch(f'{api}/git/ref/tags/{metadata["tag"]}', MAX_METADATA))["object"]
    for _ in range(4):
        if ref.get("type") != "tag":
            break
        ref = strict_json(fetch(f'{api}/git/tags/{ref["sha"]}', MAX_METADATA))["object"]
    if ref.get("type") != "commit" or ref.get("sha") != metadata["source_commit"]:
        raise ValueError("Published release tag does not identify the signed source commit")
    records = verify_release_assets(release, metadata, trust, args.output, args.debug_apk_sha256)
    args.output.mkdir(parents=True, exist_ok=True)
    for kind in ("manager", "stub"):
        asset = metadata[kind]
        data = fetch(asset["url"], asset["size"])
        if len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
            raise ValueError("Published artifact bytes differ from signed metadata")
        path = args.output / f"published-{kind}.apk"
        path.write_bytes(data)
        certificate = baseline.signer_certificate(baseline.find_build_tool("apksigner", None), path)
        if certificate != metadata["certificate_sha256"]:
            raise ValueError("Published artifact has the wrong signer")
        badging = baseline.run((baseline.find_build_tool("aapt", None), "dump", "badging", path))
        package = baseline.PACKAGE_PATTERN.search(badging)
        expected = (metadata["application_id"], str(metadata["app_version_code"]), metadata["version_name"]) if kind == "manager" else (metadata["application_id"], "1", "1.0")
        if package is None or package.groups() != expected:
            raise ValueError("Published package/version differs from signed metadata")
        sdk = baseline.SDK_PATTERN.search(badging)
        if sdk is None or sdk.group(1) != str(metadata["min_sdk"]):
            raise ValueError("Published Android requirement differs from signed metadata")
        records[kind] = {"sha256": asset["sha256"], "certificate_sha256": certificate, "size": len(data)}
    with zipfile.ZipFile(args.output / "published-manager.apk") as archive:
        if archive.read("assets/stub.apk") != (args.output / "published-stub.apk").read_bytes():
            raise ValueError("Published standalone stub is not the embedded stub")
        info = strict_json(archive.read("assets/build-info.json"))
        if info["channel"] != metadata["build_channel"]:
            raise ValueError("Published build channel mismatch")
        for key in ("source_commit", "upstream_commit", "build_id", "app_version_code", "version_name"):
            if info[key] != metadata[key]:
                raise ValueError("Published build identity mismatch")
    (args.output / "verification.json").write_text(json.dumps({"passed": True, "metadata": metadata, "artifacts": records}, indent=2) + "\n")
    print("Published metadata, APK bytes, signatures, build identity and embedded stub verified")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--config", type=Path, required=True)
    prepare_parser.add_argument("--qualification", type=Path, required=True,
                                help="release.py verification receipt for the tested bytes")
    prepare_parser.add_argument("--apk", type=Path, default=ROOT / "out/app-release.apk")
    prepare_parser.add_argument("--stub", type=Path, default=ROOT / "out/stub-release.apk")
    prepare_parser.add_argument("--channel", choices=sorted(CHANNELS), default="beta")
    prepare_parser.add_argument("--sequence", type=int, required=True)
    prepare_parser.add_argument("--days", type=int, default=14, choices=range(1, 31))
    prepare_parser.add_argument("--notes", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify-published")
    verify_parser.add_argument("--url", required=True)
    verify_parser.add_argument("--channel", choices=sorted(CHANNELS), required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    verify_parser.add_argument("--debug-apk-sha256", help="also verify an explicitly approved prerelease debug APK against its tested SHA-256")
    fixture_parser = commands.add_parser("test-fixtures", help="prepare isolated instrumentation inputs; never publish these")
    fixture_parser.add_argument("--config", type=Path, required=True)
    fixture_parser.add_argument("--apk", type=Path, default=ROOT / "out/app-release.apk")
    fixture_parser.add_argument("--stub", type=Path, default=ROOT / "out/stub-release.apk")
    fixture_parser.add_argument("--test-apk", type=Path, default=ROOT / "out/test-release.apk")
    fixture_parser.add_argument("--output", type=Path, default=ROOT / "out/update-fixtures")
    args = parser.parse_args()
    trust = strict_json((ROOT / "updates/trust.json").read_bytes())
    if args.command == "prepare":
        prepare(args, trust)
    elif args.command == "verify-published":
        verify_published(args, trust)
    else:
        fixtures(args, trust)


if __name__ == "__main__":
    main()
