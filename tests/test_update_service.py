import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.request

from tools import update_service as update


def fixture():
    trust = {"repository": "Jordan231111/KitsuneMagisk", "application_id": "io.github.jordan231111.magisk",
             "certificate_sha256": "a" * 64}
    data = {
        "schema": 1, "channel": "beta", "build_channel": "next-system-experimental", "sequence": 2, "issued_at": 1000, "expires_at": 2000,
        "application_id": trust["application_id"], "product_version": "0.1.0-beta.1", "app_version_code": 31002,
        "version_name": "0.1.0-beta.1-kitsune.12345678", "source_commit": "1" * 40, "source_digest": "2" * 64,
        "source_dirty": False, "upstream_version": "31.0", "upstream_commit": "3" * 40,
        "build_id": "4" * 64, "protocol_version": 1, "variant": "release", "certificate_sha256": "a" * 64,
        "release_notes": "Verified test release", "tag": "kitsune-v0.1.0-beta.1", "min_sdk": 23,
    }
    for name, size in [("manager", 1024), ("stub", 256)]:
        data[name] = {"url": f'https://github.com/{trust["repository"]}/releases/download/{data["tag"]}/{name}.apk',
                      "size": size, "sha256": "5" * 64}
    return trust, data


class MetadataSchemaTest(unittest.TestCase):
    def test_valid_metadata(self):
        trust, data = fixture()
        update.validate_payload(data, trust, channel="beta", now=1500)

    def test_invalid_metadata_is_rejected(self):
        trust, original = fixture()
        cases = [("schema", True), ("sequence", True), ("sequence", 0), ("sequence", 2**63),
                 ("app_version_code", 31000), ("app_version_code", 2100000001),
                 ("min_sdk", True), ("min_sdk", 0), ("min_sdk", 10001),
                 ("channel", "stable"), ("variant", "debug"), ("application_id", "untrusted.app"),
                 ("certificate_sha256", "b" * 64), ("source_commit", "a" * 8), ("source_dirty", "false"),
                 ("expires_at", 1499), ("issued_at", 2001), ("expires_at", 1000 + update.MAX_VALIDITY + 1),
                 ("release_notes", ""), ("tag", "v31.0"), ("version_name", "../bad")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                data = dict(original, **{key: value})
                with self.assertRaises(ValueError):
                    update.validate_payload(data, trust, channel="beta", now=1500)

    def test_asset_boundary_and_lengths(self):
        trust, original = fixture()
        for url in ["http://github.com/x/a.apk", "https://github.com.evil.invalid/x.apk",
                    original["manager"]["url"] + "?replacement=1", original["manager"]["url"].replace("manager.apk", "../manager.apk"),
                    original["manager"]["url"].replace("Jordan231111", "other-owner")]:
            data = copy.deepcopy(original)
            data["manager"]["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                update.validate_payload(data, trust, channel="beta", now=1500)
        for name, field, value in [("manager", "size", 0), ("manager", "size", True),
                                    ("stub", "size", 9 * 1024 * 1024), ("manager", "sha256", "not-a-digest")]:
            data = copy.deepcopy(original)
            data[name][field] = value
            with self.subTest(name=name, field=field), self.assertRaises(ValueError):
                update.validate_payload(data, trust, channel="beta", now=1500)

    def test_unknown_missing_and_duplicate_fields_fail(self):
        trust, data = fixture()
        with self.assertRaises(ValueError):
            update.validate_payload(dict(data, unknown=True), trust, channel="beta", now=1500)
        del data["source_commit"]
        with self.assertRaises(ValueError):
            update.validate_payload(data, trust, channel="beta", now=1500)
        for raw in [b'{"schema":1,"schema":1}', b'{}{}', b'[]', b'\xff', b' ' * (update.MAX_METADATA + 1)]:
            with self.subTest(raw=raw[:30]), self.assertRaises((ValueError, UnicodeError)):
                update.strict_json(raw)

    def test_redirects_do_not_change_authority_or_downgrade_https(self):
        redirect = update.Redirects()
        request = urllib.request.Request("https://github.com/Jordan231111/KitsuneMagisk/releases/download/tag/app.apk")
        for target in ["http://github.com/file", "https://attacker.invalid/file", "https://user:pass@release-assets.githubusercontent.com/file"]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                redirect.redirect_request(request, None, 302, "", {}, target)
        result = redirect.redirect_request(request, None, 302, "", {}, "https://release-assets.githubusercontent.com/file?token=public")
        self.assertEqual(result.host, "release-assets.githubusercontent.com")


class PublishedAssetsTest(unittest.TestCase):
    def setUp(self):
        self.trust, self.metadata = fixture()
        self.release = {"prerelease": True, "assets": [{"name": "manager.apk"}, {"name": "stub.apk"}]}
        self.debug = b"locally tested debug APK"
        self.digest = hashlib.sha256(self.debug).hexdigest()
        self.temporary = tempfile.TemporaryDirectory(prefix="kitsune-publication-test-")
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def verify(self, digest=None):
        return update.verify_release_assets(self.release, self.metadata, self.trust, self.output, digest)

    def test_release_only_needs_no_debug_pin(self):
        with mock.patch.object(update, "fetch") as fetch:
            self.assertEqual(self.verify(), {})
            fetch.assert_not_called()

    def test_debug_requires_explicit_pin_and_exact_published_bytes(self):
        self.release["assets"].append({"name": "app-debug.apk"})
        with mock.patch.object(update, "fetch", return_value=self.debug) as fetch:
            with self.assertRaises(ValueError):
                self.verify()
            fetch.assert_not_called()
            records = self.verify(self.digest)
            self.assertEqual(records["debug"], {"sha256": self.digest, "size": len(self.debug), "update_feed": False})
            self.assertEqual((self.output / "published-debug.apk").read_bytes(), self.debug)
            fetch.assert_called_once_with(
                f'https://github.com/{self.trust["repository"]}/releases/download/{self.metadata["tag"]}/app-debug.apk',
                200 * 1024 * 1024)

    def test_changed_debug_bytes_are_rejected_before_writing(self):
        self.release["assets"].append({"name": "app-debug.apk"})
        with mock.patch.object(update, "fetch", return_value=self.debug + b"changed"):
            with self.assertRaises(ValueError):
                self.verify(self.digest)
        self.assertFalse((self.output / "published-debug.apk").exists())

    def test_pin_does_not_approve_missing_or_extra_apks(self):
        for names in (["manager.apk", "stub.apk"], ["manager.apk", "app-debug.apk"],
                      ["manager.apk", "stub.apk", "app-debug.apk", "test.apk"]):
            self.release["assets"] = [{"name": name} for name in names]
            with self.subTest(names=names), mock.patch.object(update, "fetch") as fetch:
                with self.assertRaises(ValueError):
                    self.verify(self.digest)
                fetch.assert_not_called()

    def test_regular_release_accepts_exact_debug_companion(self):
        self.release["prerelease"] = False
        self.release["assets"].append({"name": "app-debug.apk"})
        with mock.patch.object(update, "fetch", return_value=self.debug):
            records = self.verify(self.digest)
        self.assertEqual(records["debug"]["sha256"], self.digest)
        self.assertFalse(records["debug"]["update_feed"])

    def test_debug_pin_requires_valid_digest(self):
        self.release["assets"].append({"name": "app-debug.apk"})
        for digest in ["", "x" * 64, self.digest + "\n"]:
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.verify(digest)


@unittest.skipUnless(shutil.which("java") and shutil.which("keytool"), "Requires JDK for actual signature tests")
class MetadataSignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="kitsune-update-signature-test-")
        cls.root = Path(cls.temporary.name)
        cls.config = cls.root / "key.prop"
        key = cls.root / "test.p12"
        env = dict(os.environ, KITSUNE_TEST_PASSWORD="temporary-fixture-only")
        subprocess.run(["keytool", "-genkeypair", "-noprompt", "-keystore", str(key), "-storepass:env", "KITSUNE_TEST_PASSWORD",
                        "-keypass:env", "KITSUNE_TEST_PASSWORD", "-alias", "test", "-keyalg", "RSA", "-keysize", "3072",
                        "-validity", "2", "-dname", "CN=Disposable metadata test"], env=env, check=True, capture_output=True)
        cls.config.write_text(f"keyStore={key}\nkeyStorePass=temporary-fixture-only\nkeyAlias=test\nkeyPass=temporary-fixture-only\n")
        cls.config.chmod(0o600)
        cert = subprocess.check_output(["keytool", "-exportcert", "-keystore", str(key), "-storepass:env", "KITSUNE_TEST_PASSWORD", "-alias", "test"], env=env)
        cls.trust, cls.data = fixture()
        cls.trust.update(certificate=base64.b64encode(cert).decode(), certificate_sha256=hashlib.sha256(cert).hexdigest())
        cls.data["certificate_sha256"] = cls.trust["certificate_sha256"]
        cls.signed = update.envelope(cls.data, cls.config, cls.trust)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_signature_and_expired_refresh(self):
        self.assertEqual(update.verify_envelope(self.signed, self.trust, channel="beta", now=1500), self.data)
        with self.assertRaises(ValueError):
            update.verify_envelope(self.signed, self.trust, channel="beta", now=3000)
        self.assertEqual(update.verify_envelope(self.signed, self.trust, channel="beta", now=3000, historical=True), self.data)

    def test_changed_payload_and_signature_are_rejected(self):
        for key in ("payload", "signature"):
            outer = json.loads(self.signed)
            raw = bytearray(base64.b64decode(outer[key]))
            raw[0] ^= 1
            outer[key] = base64.b64encode(raw).decode()
            with self.subTest(key=key), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                update.verify_envelope(json.dumps(outer).encode(), self.trust, channel="beta", now=1500)

    def test_unknown_key_and_channel_are_rejected(self):
        with self.assertRaises(ValueError):
            update.verify_envelope(self.signed, dict(self.trust, certificate_sha256="0" * 64), channel="beta", now=1500)
        with self.assertRaises(ValueError):
            update.verify_envelope(self.signed, self.trust, channel="stable", now=1500)


if __name__ == "__main__":
    unittest.main()
