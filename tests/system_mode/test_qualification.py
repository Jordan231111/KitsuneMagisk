from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
from pathlib import Path
import os
import time
import shlex
import shutil
import signal
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid
import zipfile

from tools.system_mode.authorization import digest_backup_path
from tools.system_mode.doctor import CommandResult, ProbeError, classify_report, fixture_report
from tools.system_mode.qualification import (
    QUALIFICATION_KEY_ENV,
    IndeterminateLifecycleError,
    _canonical_bytes,
    _policy_from_apk,
    _policy_bootstrap,
    _checked_command,
    _inventory_digest,
    _exec_result_payload,
    _remote_database_digest,
    _sqlite_content_digest,
    _verify_init_exec_probe,
    _recover_from_candidates,
    _remove_verified_qualification_backups,
    _failure_recovery_candidates,
    _restore_unchanged_backup,
    _boot_critical_roots,
    _run_host,
    _seal_record,
    _sha256,
    evidence_from_record,
    instance_identity,
    load_qualification_evidence,
    load_qualification_record,
    qualify_target,
    stable_target_digest,
    validate_qualification_record,
    verify_report_qualification,
    verify_report_qualification_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tools" / "system_mode" / "fixtures" / "mumu-writable.json"


class QualificationTest(unittest.TestCase):
    def test_policy_apk_requires_the_clean_source_and_target_abi(self) -> None:
        policy = bytearray(120)
        policy[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", policy, 18, 183)
        struct.pack_into("<Q", policy, 32, 64)
        struct.pack_into("<HH", policy, 54, 56, 1)
        struct.pack_into("<I", policy, 64, 1)
        struct.pack_into("<Q", policy, 112, 16384)
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "candidate.apk"
            def write_apk(dirty="false", binary=policy):
                with zipfile.ZipFile(apk, "w") as archive:
                    archive.writestr("lib/arm64-v8a/libmagiskpolicy.so", binary)
                    archive.writestr("assets/util_functions.sh",
                                     f"KITSUNE_SOURCE_COMMIT='{commit}'\nKITSUNE_SOURCE_DIRTY={dirty}\n")
            write_apk()
            digest, captured = _policy_from_apk(apk, "arm64-v8a", commit)
            self.assertEqual(hashlib.sha256(apk.read_bytes()).hexdigest(), digest)
            self.assertEqual(policy, captured)
            with self.assertRaisesRegex(ValueError, "source commit"):
                _policy_from_apk(apk, "arm64-v8a", "b" * 40)
            write_apk(dirty="true")
            with self.assertRaisesRegex(ValueError, "source commit"):
                _policy_from_apk(apk, "arm64-v8a", commit)
            write_apk(binary=b"not an ELF")
            with self.assertRaises(ValueError):
                _policy_from_apk(apk, "arm64-v8a", commit)

    def test_policy_bootstrap_requires_matching_bytes_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = Path(temporary) / "policy with 'quotes'"
            for code, corrupt, expected in ((0, False, 0), (8, False, 67), (0, True, 66)):
                with self.subTest(code=code, corrupt=corrupt):
                    policy.write_text(f"#!/bin/sh\n[ \"$*\" = '--live --magisk' ] || exit 9\nexit {code}\n")
                    policy.chmod(0o755)
                    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
                    if corrupt:
                        policy.write_text(policy.read_text() + "# changed\n")
                    script = "set -eu\nrole=init\n"
                    if shutil.which("sha256sum") is None:
                        script += 'sha256sum() { shasum -a 256 "$@"; }\n'
                    script += _policy_bootstrap(str(policy), digest) + "printf proof\n"
                    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=10)
                    self.assertEqual(expected, result.returncode, result.stderr)
                    self.assertEqual("proof" if expected == 0 else "", result.stdout)

    def test_domain_probe_waits_for_both_results_and_rejects_invalid_output(self) -> None:
        nonce = "1" * 32
        boot = "11111111-1111-1111-1111-111111111111"
        paths = {"init": "/dev/init.result", "magisk": "/dev/magisk.result"}
        probe = {"path": "/system/etc/init/probe.rc"}
        helper = {"path": "/system/etc/init/helper.sh"}
        evidence = {probe["path"]: probe, helper["path"]: helper}
        for role, path in paths.items():
            payload = _exec_result_payload(nonce, role, boot, f"u:r:{role}:s0")
            evidence[path] = {"path": path, "sha256": hashlib.sha256(payload).hexdigest(),
                              "size": len(payload), "mode": "0600", "uid": 0, "gid": 0}
        client = mock.Mock()
        pending = CommandResult(stdout="", stderr="", returncode=1)
        ready = CommandResult(stdout=f"{nonce}:{boot}", stderr="", returncode=0)
        client.shell.side_effect = [pending, ready, ready]
        with mock.patch("tools.system_mode.qualification._remote_file_evidence", side_effect=lambda _, path: evidence[path]), \
             mock.patch("tools.system_mode.qualification.time.sleep") as sleep:
            result = _verify_init_exec_probe(client, probe=probe, helper=helper,
                                            result_paths=paths, nonce=nonce, boot_id=boot)
            self.assertEqual(hashlib.sha256(boot.encode()).hexdigest(), result["boot_id_sha256"])
            sleep.assert_called_once_with(0.25)
            client.shell.side_effect = None
            client.shell.return_value = ready
            evidence[paths["magisk"]]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ProbeError, "did not execute the magisk domain probe"):
                _verify_init_exec_probe(client, probe=probe, helper=helper,
                                        result_paths=paths, nonce=nonce, boot_id=boot)
        client.shell.return_value = pending
        with mock.patch("tools.system_mode.qualification.time.monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(ProbeError, "timed out waiting"):
                _verify_init_exec_probe(client, probe=probe, helper=helper,
                                        result_paths=paths, nonce=nonce, boot_id=boot)

    def test_database_digest_preserves_schema_rows_and_value_types(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-sqlite-content-") as temporary:
            path = Path(temporary) / "magisk.db"
            with sqlite3.connect(path) as database:
                database.executescript(
                    "CREATE TABLE settings(key TEXT PRIMARY KEY, value);"
                    "CREATE TABLE policies(uid INT PRIMARY KEY, policy INT);"
                    "INSERT INTO settings VALUES('denylist',0),('zygisk',1);"
                    "INSERT INTO policies VALUES(2000,2);"
                    "PRAGMA user_version=12;"
                )
                database.execute("INSERT INTO settings VALUES(?,?)", ('quoted"key', b"\0\xff\n"))
            before = path.read_bytes()
            expected = _sqlite_content_digest(before)
            with sqlite3.connect(path) as database:
                database.execute("REPLACE INTO settings VALUES('denylist',0)")
            after = path.read_bytes()
            self.assertNotEqual(hashlib.sha256(before).digest(), hashlib.sha256(after).digest())
            self.assertEqual(expected, _sqlite_content_digest(after))
            for statement in (
                "UPDATE policies SET policy=1 WHERE uid=2000",
                "DELETE FROM policies",
                "UPDATE settings SET value='0' WHERE key='denylist'",
                "PRAGMA user_version=13",
                "PRAGMA application_id=1",
                "CREATE TABLE extra(id INTEGER PRIMARY KEY)",
            ):
                with self.subTest(statement=statement):
                    path.write_bytes(before)
                    with sqlite3.connect(path) as database:
                        database.execute(statement)
                    self.assertNotEqual(expected, _sqlite_content_digest(path.read_bytes()))
            with self.assertRaises(ProbeError):
                _sqlite_content_digest(before[:100])

    def test_database_capture_requires_stable_bytes_metadata_and_no_journal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-sqlite-capture-") as temporary:
            path = Path(temporary) / "magisk.db"
            with sqlite3.connect(path) as database:
                database.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value INT)")
            data = path.read_bytes()
            node = {
                "path": "/data/adb/magisk.db", "kind": "file", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "mode": "0600",
                "uid": 0, "gid": 0, "mtime_epoch": 1, "selinux_context": None,
            }
            metadata = {key: val for key, val in node.items() if key not in {"kind", "mtime_epoch"}}
            encoded = base64.b64encode(data).decode("ascii")
            with mock.patch("tools.system_mode.qualification._root_checked", side_effect=[encoded, ""]) as root, \
                 mock.patch("tools.system_mode.qualification._remote_file_evidence", return_value=metadata):
                self.assertEqual(_sqlite_content_digest(data), _remote_database_digest(object(), node))
                for suffix in ("-journal", "-wal", "-shm"):
                    self.assertIn(f"[ ! -e /data/adb/magisk.db{suffix} ]", root.call_args_list[0].args[1])
                    self.assertIn(f"[ ! -L /data/adb/magisk.db{suffix} ]", root.call_args_list[1].args[1])
            with mock.patch("tools.system_mode.qualification._root_checked", return_value=encoded), \
                 mock.patch("tools.system_mode.qualification._remote_file_evidence", return_value=dict(metadata, uid=1)):
                with self.assertRaisesRegex(ProbeError, "metadata changed"):
                    _remote_database_digest(object(), node)
            with mock.patch("tools.system_mode.qualification._root_checked", return_value=encoded):
                with self.assertRaisesRegex(ProbeError, "changed during inventory"):
                    _remote_database_digest(object(), dict(node, sha256="0" * 64))

    def test_database_inventory_keeps_permissions_and_other_files_exact(self) -> None:
        node = {
            "path": "/data/adb/magisk.db", "kind": "file", "size": 4096,
            "sha256": "1" * 64, "sqlite_content_sha256": "2" * 64,
            "mode": "0600", "uid": 0, "gid": 0, "mtime_epoch": 1,
            "selinux_context": "u:object_r:adb_data_file:s0",
        }
        expected = _inventory_digest([node])
        self.assertEqual(expected, _inventory_digest([dict(node, sha256="3" * 64, size=8192, mtime_epoch=2)]))
        for key, val in (("mode", "0644"), ("uid", 1), ("gid", 1),
                         ("selinux_context", None), ("sqlite_content_sha256", "4" * 64)):
            self.assertNotEqual(expected, _inventory_digest([dict(node, **{key: val})]))
        with self.assertRaisesRegex(ValueError, "only valid for the Magisk database"):
            _inventory_digest([dict(node, path="/system/etc/init/magisk.rc")])
        ordinary = {key: val for key, val in node.items() if key != "sqlite_content_sha256"}
        self.assertNotEqual(_inventory_digest([ordinary]), _inventory_digest([dict(ordinary, sha256="3" * 64)]))

    def tearDown(self) -> None:
        os.environ.pop(QUALIFICATION_KEY_ENV, None)

    @staticmethod
    def executable(root: Path, name: str) -> Path:
        path = root / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        path.chmod(0o700)
        return path

    def record(self, root: Path) -> tuple[dict[str, object], Path, Path, Path]:
        root = root.resolve()
        seal_key = root / "qualification.key"
        os.environ[QUALIFICATION_KEY_ENV] = str(seal_key)
        identity_path = root / "instance.json"
        identity_path.write_text('{"instance":0}\n', encoding="ascii")
        backup = root / "external-backup.img"
        backup.write_bytes(b"exact external recovery")
        identity = instance_identity(identity_path)
        commands = {
            "backup": _checked_command(
                f"{self.executable(root, 'backup.sh')} '{{backup}}'",
                "backup",
                backup_placeholder=True,
            ),
            "cold_boot": _checked_command(
                str(self.executable(root, "cold_boot.sh")),
                "cold boot",
            ),
            "restore": _checked_command(
                f"{self.executable(root, 'restore.sh')} '{{backup}}'",
                "restore",
                backup_placeholder=True,
            ),
        }
        contract = "8" * 64
        record_id = "11111111-1111-1111-1111-111111111111"
        anchor_before = {
            "path": f"/system/etc/init/.kitsune-system-mode-backup-anchor-{record_id}",
            "sha256": "d" * 64,
            "size": 16,
            "mode": "0600",
            "uid": 0,
            "gid": 0,
            "selinux_context": "u:object_r:system_file:s0",
        }
        anchor_mutated = dict(anchor_before, sha256="e" * 64)
        data_anchor_before = dict(
            anchor_before,
            path=f"/data/adb/.kitsune-system-mode-backup-anchor-{record_id}",
            sha256="8" * 64,
            selinux_context="u:object_r:adb_data_file:s0",
        )
        data_anchor_mutated = dict(data_anchor_before, sha256="9" * 64)
        inventory = [
            {
                "path": path,
                "kind": "absent",
                "sha256": None,
                "size": None,
                "mode": None,
                "uid": None,
                "gid": None,
                "mtime_epoch": None,
                "selinux_context": None,
            }
            for path in _boot_critical_roots("/system/etc/init")
        ]
        init_result_path = (
            f"/dev/.kitsune-system-mode-qualification-{record_id}-init.result"
        )
        magisk_result_path = (
            f"/dev/.kitsune-system-mode-qualification-{record_id}-magisk.result"
        )

        def result(path: str, digest: str) -> dict[str, object]:
            return {
                "path": path,
                "sha256": digest,
                "size": 128,
                "mode": "0600",
                "uid": 0,
                "gid": 0,
                "selinux_context": "u:object_r:device:s0",
            }

        record: dict[str, object] = {
            "schema_version": 1,
            "record_id": record_id,
            "generated_at": "2026-08-01T12:00:00Z",
            "adapter": {
                "id": "generic-in-guest",
                "execution": "in_guest",
                "clone_authorized": False,
            },
            "source": {"repository_commit": "1" * 40, "probe_sha256": "2" * 64},
            "target": {
                "serial_sha256": "3" * 64,
                "fingerprint_sha256": "4" * 64,
                "api": 32,
                "abis": ["x86_64"],
                "baseline_contract_sha256": contract,
                "baseline_inventory": inventory,
                "baseline_inventory_sha256": _inventory_digest(inventory),
            },
            "instance_identity": identity,
            "commands": commands,
            "backup": {
                "snapshot_id": "external-vm0-baseline",
                "location": str(backup),
                "sha256_before": digest_backup_path(backup),
                "sha256_after": digest_backup_path(backup),
                "backup_command": shlex.join(
                    [
                        str(commands["backup"]["argv"][0]),
                        str(backup),
                    ]
                ),
                "restore_command": shlex.join(
                    [
                        str(commands["restore"]["argv"][0]),
                        str(backup),
                    ]
                ),
                "created_boot_id_sha256": "f" * 64,
                "qualification_residue": "absent",
            },
            "challenge": {
                "sha256_before": "0" * 64,
                "sha256_after": "0" * 64,
                "created_boot_id_sha256": "d" * 64,
                "restored_boot_id_sha256": "e" * 64,
                "anchor_before": anchor_before,
                "anchor_mutated": anchor_mutated,
                "anchor_after_restore": dict(anchor_before),
                "marker": {
                    "path": (
                        "/system/etc/init/"
                        f".kitsune-system-mode-recovery-{record_id}"
                    ),
                    "sha256": "7" * 64,
                    "size": 16,
                    "mode": "0600",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:system_file:s0",
                },
                "data_anchor_before": data_anchor_before,
                "data_anchor_mutated": data_anchor_mutated,
                "data_anchor_after_restore": dict(data_anchor_before),
                "data_marker": {
                    "path": f"/data/adb/.kitsune-system-mode-recovery-{record_id}",
                    "sha256": "a" * 64,
                    "size": 16,
                    "mode": "0600",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:adb_data_file:s0",
                },
                "temporary_backup_removed": True,
            },
            "init": {
                "directory": "/system/etc/init",
                "probe": {
                    "path": (
                        "/system/etc/init/"
                        f"kitsune-system-mode-qualification-{record_id}.rc"
                    ),
                    "sha256": "5" * 64,
                    "size": 16,
                    "mode": "0644",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:system_file:s0",
                },
                "helper": {
                    "path": (
                        "/system/etc/init/"
                        f".kitsune-system-mode-qualification-{record_id}.sh"
                    ),
                    "sha256": "6" * 64,
                    "size": 256,
                    "mode": "0755",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:system_file:s0",
                },
                "property": "kitsune.system_mode.qualify",
                "nonce_sha256": "6" * 64,
                "domains": {
                    "init": "u:r:init:s0",
                    "magisk": "u:r:magisk:s0",
                },
            },
            "persistence": {
                "backing_write_probe": "passed",
                "boot_evidence": "host-command-new-boot-id-and-init-and-magisk-exec",
                "cold_boot_command": shlex.join(commands["cold_boot"]["argv"]),
                "cold_boot_ids": ["a" * 64, "b" * 64, "c" * 64],
                "host_restart_ids": [],
                "exec_proofs": [
                    {
                        "boot_id_sha256": boot,
                        "init_result": result(init_result_path, digest),
                        "magisk_result": result(magisk_result_path, other),
                    }
                    for boot, digest, other in (
                        ("a" * 64, "1" * 64, "4" * 64),
                        ("b" * 64, "2" * 64, "5" * 64),
                        ("c" * 64, "3" * 64, "6" * 64),
                    )
                ],
            },
            "recovery": {
                "marker": {
                    "path": (
                        "/system/etc/init/"
                        f".kitsune-system-mode-final-recovery-{record_id}"
                    ),
                    "sha256": "7" * 64,
                    "size": 16,
                    "mode": "0600",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:system_file:s0",
                },
                "data_marker": {
                    "path": f"/data/adb/.kitsune-system-mode-final-recovery-{record_id}",
                    "sha256": "b" * 64,
                    "size": 16,
                    "mode": "0600",
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:adb_data_file:s0",
                },
                "restore_executed": True,
                "qualification_paths_absent_after_restore": True,
                "restored_boot_id_sha256": "9" * 64,
                "target_contract_sha256_after_restore": contract,
                "inventory_after_restore": copy.deepcopy(inventory),
                "inventory_sha256_after_restore": _inventory_digest(inventory),
            },
        }
        _seal_record(record, seal_key)
        record_path = root / "qualification.json"
        record_path.write_bytes(_canonical_bytes(record))
        return record, record_path, backup, identity_path

    def test_host_commands_are_absolute_and_backup_destination_is_new(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute executable"):
            _checked_command("relative-wrapper backup", "backup")

        with tempfile.TemporaryDirectory(prefix="kitsune-existing-backup-") as temp:
            root = Path(temp)
            backup = root / "backup.img"
            backup.write_bytes(b"already exists")
            with self.assertRaisesRegex(ValueError, "must not exist"):
                qualify_target(
                    mock.Mock(),
                    output=root / "qualification.json",
                    backup_location=backup,
                    snapshot_id="existing",
                    backup_command="/bin/false",
                    restore_command="/bin/false",
                    cold_boot_command="/bin/false",
                    instance_identity_path=backup,
                )

    def test_failed_recovery_never_deletes_qualification_backups(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-retained-recovery-") as temp:
            root = Path(temp)
            for failure in ("restore", "identity", "inventory"):
                challenge = root / f"challenge-{failure}.img"
                canonical = root / f"canonical-{failure}.img"
                challenge.write_bytes(b"challenge")
                canonical.write_bytes(b"canonical")
                error = ProbeError(f"{failure} verification failed")
                returned = _remove_verified_qualification_backups(
                    challenge,
                    canonical,
                    final_ready=False,
                    qualification_succeeded=False,
                    recovery_verified=True,
                    lifecycle_indeterminate=False,
                    cleanup_error=error,
                )
                self.assertIs(error, returned)
                self.assertTrue(challenge.is_file())
                self.assertTrue(canonical.is_file())

            challenge = root / "challenge-indeterminate.img"
            canonical = root / "canonical-indeterminate.img"
            challenge.write_bytes(b"challenge")
            canonical.write_bytes(b"canonical")
            self.assertIsNone(
                _remove_verified_qualification_backups(
                    challenge,
                    canonical,
                    final_ready=False,
                    qualification_succeeded=False,
                    recovery_verified=False,
                    lifecycle_indeterminate=True,
                    cleanup_error=None,
                )
            )
            self.assertTrue(challenge.is_file())
            self.assertTrue(canonical.is_file())

    def test_verified_recovery_removes_only_disposable_backups(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-verified-recovery-") as temp:
            root = Path(temp)
            challenge = root / "challenge.img"
            canonical = root / "canonical.img"
            challenge.write_bytes(b"challenge")
            canonical.write_bytes(b"canonical")
            self.assertIsNone(
                _remove_verified_qualification_backups(
                    challenge,
                    canonical,
                    final_ready=True,
                    qualification_succeeded=True,
                    recovery_verified=True,
                    lifecycle_indeterminate=False,
                    cleanup_error=None,
                )
            )
            self.assertFalse(challenge.exists())
            self.assertTrue(canonical.is_file())

    def test_restore_hashes_before_and_after_and_rejects_changed_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-restore-order-") as temp:
            backup = Path(temp) / "backup.img"
            backup.write_bytes(b"baseline")
            expected = digest_backup_path(backup)
            events: list[str] = []

            def digest(path: Path) -> str:
                events.append("digest")
                return digest_backup_path(path)

            with mock.patch(
                "tools.system_mode.authorization.digest_backup_path",
                side_effect=digest,
            ):
                _restore_unchanged_backup(
                    backup,
                    expected,
                    "test",
                    lambda: events.append("restore"),
                )
            self.assertEqual(["digest", "restore", "digest"], events)

            backup.write_bytes(b"changed")
            restored = False

            def restore() -> None:
                nonlocal restored
                restored = True

            with self.assertRaisesRegex(ValueError, "changed before restore"):
                _restore_unchanged_backup(backup, expected, "test", restore)
            self.assertFalse(restored)

            backup.write_bytes(b"baseline")
            expected = digest_backup_path(backup)
            with self.assertRaisesRegex(ValueError, "changed during restore"):
                _restore_unchanged_backup(
                    backup,
                    expected,
                    "test",
                    lambda: backup.write_bytes(b"mutated by restore"),
                )

    def test_failure_recovery_selects_only_proven_backups(self) -> None:
        canonical = Path("/qualified/final")
        challenge = Path("/qualified/challenge")
        common = {
            "canonical_backup": canonical,
            "challenge_backup": challenge,
            "final_digest": "f" * 64,
            "challenge_digest": "c" * 64,
        }
        self.assertEqual(
            [],
            _failure_recovery_candidates(
                final_verified=False,
                challenge_verified=False,
                **common,
            ),
        )
        self.assertEqual(
            [("challenge", challenge, "c" * 64)],
            _failure_recovery_candidates(
                final_verified=False,
                challenge_verified=True,
                **common,
            ),
        )
        self.assertEqual(
            [
                ("clean", canonical, "f" * 64),
                ("challenge", challenge, "c" * 64),
            ],
            _failure_recovery_candidates(
                final_verified=True,
                challenge_verified=True,
                **common,
            ),
        )

    def test_corrupt_final_recovery_falls_back_to_verified_challenge(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-recovery-fallback-") as temp:
            root = Path(temp)
            canonical = root / "final.img"
            challenge = root / "challenge.img"
            canonical.write_bytes(b"clean")
            challenge.write_bytes(b"challenge")
            final_digest = digest_backup_path(canonical)
            challenge_digest = digest_backup_path(challenge)
            candidates = _failure_recovery_candidates(
                final_verified=True,
                challenge_verified=True,
                canonical_backup=canonical,
                challenge_backup=challenge,
                final_digest=final_digest,
                challenge_digest=challenge_digest,
            )
            canonical.write_bytes(b"corrupt")
            restores: list[str] = []
            direct_cleanup_called = False

            def recover(candidate: tuple[str, Path, str]) -> None:
                name, backup, expected = candidate
                _restore_unchanged_backup(
                    backup,
                    expected,
                    name,
                    lambda: restores.append(name),
                )

            def direct_cleanup() -> None:
                nonlocal direct_cleanup_called
                direct_cleanup_called = True

            self.assertEqual(
                "challenge",
                _recover_from_candidates(candidates, recover, direct_cleanup),
            )
            self.assertEqual(["challenge"], restores)
            self.assertFalse(direct_cleanup_called)

    def test_failed_candidates_use_direct_cleanup_but_indeterminate_stops(self) -> None:
        candidates = [
            ("clean", Path("/qualified/final"), "f" * 64),
            ("challenge", Path("/qualified/challenge"), "c" * 64),
        ]
        attempts: list[str] = []

        def fail(candidate: tuple[str, Path, str]) -> None:
            attempts.append(candidate[0])
            raise ValueError("candidate changed")

        self.assertIsNone(
            _recover_from_candidates(
                candidates,
                fail,
                lambda: attempts.append("direct"),
            )
        )
        self.assertEqual(["clean", "challenge", "direct"], attempts)

        attempts.clear()

        def indeterminate(candidate: tuple[str, Path, str]) -> None:
            attempts.append(candidate[0])
            raise IndeterminateLifecycleError("unknown lifecycle state")

        with self.assertRaises(IndeterminateLifecycleError):
            _recover_from_candidates(
                candidates,
                indeterminate,
                lambda: attempts.append("direct"),
            )
        self.assertEqual(["clean"], attempts)

    def test_live_state_stays_dirty_until_inventory_and_contract_are_verified(self) -> None:
        source = inspect.getsource(qualify_target)
        challenge_cleanup = source[
            source.index("# The challenge snapshot intentionally contains the anchor.") :
            source.index('run_lifecycle("backup", "clean backup creation"')
        ]
        clean = challenge_cleanup.index("clean_report = collect_report")
        contract = challenge_cleanup.index("stable_target_digest(clean_report)")
        inventory = challenge_cleanup.index("_boot_critical_inventory")
        clean_state = challenge_cleanup.index("live_dirty = False")
        self.assertLess(inventory, clean_state)
        self.assertLess(clean, clean_state)
        self.assertLess(contract, clean_state)

        clean_backup = source[
            source.index('live_dirty = True\n        run_lifecycle("backup", "clean backup creation"') :
            source.index("final_marker_evidence = _write_probe")
        ]
        self.assertLess(
            clean_backup.index("after_clean_backup = collect_report"),
            clean_backup.index("live_dirty = False"),
        )
        self.assertLess(
            clean_backup.index("stable_target_digest(after_clean_backup)"),
            clean_backup.index("live_dirty = False"),
        )
        self.assertLess(
            clean_backup.index("stable_target_digest(after_clean_backup)"),
            clean_backup.index("final_verified = True"),
        )
        recovery = source[source.index("finally:") : source.index("record: dict")]
        self.assertIn("_failure_recovery_candidates(", recovery)
        self.assertIn("final_verified=final_verified", recovery)
        self.assertIn("challenge_verified=challenge_verified", recovery)
        self.assertIn("qualification_succeeded=qualification_succeeded", recovery)

    def test_record_produces_evidence_only_while_backup_and_identity_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-qualification-") as temp:
            root = Path(temp)
            record, record_path, backup, identity_path = self.record(root)
            validate_qualification_record(record)
            loaded, raw, digest, canonical = load_qualification_record(record_path)
            self.assertEqual(record, loaded)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            self.assertEqual(record_path.resolve(), canonical)
            evidence = evidence_from_record(record_path)
            self.assertTrue(evidence.recovery_is_proven)
            self.assertTrue(evidence.persistence_is_proven)
            self.assertEqual("generic-in-guest", evidence.adapter_id)
            self.assertEqual(
                _sha256(_canonical_bytes(record["instance_identity"])),
                evidence.instance_identity_sha256,
            )

            backup.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "backup is missing or changed"):
                evidence_from_record(record_path)
            backup.write_bytes(b"exact external recovery")
            identity_path.write_text('{"instance":1}\n', encoding="ascii")
            with self.assertRaisesRegex(ValueError, "identity changed"):
                evidence_from_record(record_path)

    def test_record_rejects_restore_executable_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-command-drift-") as temp:
            root = Path(temp)
            record, record_path, _, _ = self.record(root)
            restore = Path(record["commands"]["restore"]["executable"]["path"])
            restore.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
            restore.chmod(0o700)
            with self.assertRaisesRegex(ValueError, "changed after qualification"):
                evidence_from_record(record_path)

    def test_record_seal_rejects_manual_edit_wrong_key_and_unsafe_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-sealed-record-") as temp:
            root = Path(temp).resolve()
            record, record_path, _, _ = self.record(root)
            edited = copy.deepcopy(record)
            edited["generated_at"] = "2026-08-02T12:00:00Z"
            record_path.write_bytes(_canonical_bytes(edited))
            with self.assertRaisesRegex(ValueError, "seal is invalid"):
                load_qualification_record(record_path)

            record_path.write_bytes(_canonical_bytes(record))
            wrong_key = root / "wrong.key"
            wrong_key.write_bytes(b"x" * 32)
            wrong_key.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "trusted host key"):
                load_qualification_record(record_path, seal_key_path=wrong_key)

            trusted_key = Path(os.environ[QUALIFICATION_KEY_ENV])
            trusted_key.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "group/world"):
                load_qualification_record(record_path)
            trusted_key.chmod(0o600)
            root.chmod(0o777)
            try:
                with self.assertRaisesRegex(ValueError, "parent must be owner-only"):
                    load_qualification_record(record_path)
            finally:
                root.chmod(0o700)

    def test_lifecycle_executes_the_opened_wrapper_inode(self) -> None:
        from tools.system_mode import qualification as module

        with tempfile.TemporaryDirectory(prefix="kitsune-pinned-command-") as temp:
            root = Path(temp).resolve()
            marker = root / "marker"
            wrapper = root / "wrapper.sh"
            wrapper.write_text(
                "#!/bin/sh\nprintf 'pinned' > \"$1\"\n",
                encoding="ascii",
            )
            wrapper.chmod(0o700)
            command = _checked_command(
                f"{shlex.quote(str(wrapper))} {shlex.quote(str(marker))}",
                "cold boot",
            )
            real_popen = module.subprocess.Popen

            def replace_then_run(*args: object, **kwargs: object):
                displaced = root / "opened-wrapper.sh"
                wrapper.rename(displaced)
                wrapper.write_text(
                    "#!/bin/sh\nprintf 'replacement' > \"$1\"\n",
                    encoding="ascii",
                )
                wrapper.chmod(0o700)
                return real_popen(*args, **kwargs)

            with mock.patch(
                "tools.system_mode.qualification.subprocess.Popen",
                side_effect=replace_then_run,
            ):
                with self.assertRaisesRegex(ValueError, "changed"):
                    _run_host(command, "cold boot", 10)
            self.assertEqual("pinned", marker.read_text(encoding="ascii"))

    def test_lifecycle_timeout_stops_the_wrapper_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-command-timeout-") as temp:
            root = Path(temp).resolve()
            pid_file = root / "child.pid"
            wrapper = root / "timeout.sh"
            wrapper.write_text(
                "#!/bin/sh\nsleep 60 &\nchild=$!\nprintf '%s' \"$child\" > \"$1\"\nwait \"$child\"\n",
                encoding="ascii",
            )
            wrapper.chmod(0o700)
            command = _checked_command(
                f"{shlex.quote(str(wrapper))} {shlex.quote(str(pid_file))}",
                "cold boot",
            )
            with mock.patch(
                "tools.system_mode.qualification.os.killpg",
                wraps=os.killpg,
            ) as kill_group:
                with self.assertRaisesRegex(ProbeError, "entire process group"):
                    _run_host(command, "cold boot", 1)
            self.assertTrue(
                any(call.args[1] == signal.SIGKILL for call in kill_group.call_args_list)
            )
            child = int(pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("timed-out lifecycle grandchild remained alive")

    def test_lifecycle_cancellation_stops_children_before_recovery(self) -> None:
        for cancellation in (KeyboardInterrupt, SystemExit):
            with self.subTest(cancellation=cancellation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                marker = root / "started"
                wrapper = root / "cancel.sh"
                wrapper.write_text(
                    '#!/bin/sh\nsleep 60 &\nprintf started > "$1"\nwait\n',
                    encoding="ascii",
                )
                wrapper.chmod(0o700)
                command = _checked_command(
                    f"{shlex.quote(str(wrapper))} {shlex.quote(str(marker))}", "cold boot"
                )
                original = subprocess.Popen.communicate
                processes = []

                def cancel_once(process, *args, **kwargs):
                    if not processes:
                        processes.append(process)
                        deadline = time.monotonic() + 5
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(marker.exists())
                        raise cancellation()
                    return original(process, *args, **kwargs)

                try:
                    with mock.patch.object(subprocess.Popen, "communicate", cancel_once):
                        with self.assertRaises(cancellation):
                            _run_host(command, "cold boot", 30)
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(processes[0].pid, 0)
                finally:
                    for process in processes:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)

    def test_lifecycle_refuses_recovery_when_group_death_is_unproven(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-command-indeterminate-") as temp:
            root = Path(temp).resolve()
            wrapper = root / "timeout.sh"
            wrapper.write_text("#!/bin/sh\nsleep 60\n", encoding="ascii")
            wrapper.chmod(0o700)
            command = _checked_command(str(wrapper), "cold boot")
            with mock.patch(
                "tools.system_mode.qualification._wait_process_group_gone",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    IndeterminateLifecycleError,
                    "could not be proven stopped",
                ):
                    _run_host(command, "cold boot", 1)

    def test_record_rejects_inventory_escape_and_reused_domain_proof(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-inventory-record-") as temp:
            record, _, _, _ = self.record(Path(temp))
            escaped = copy.deepcopy(record)
            escaped["target"]["baseline_inventory"].append(
                {
                    "path": "/data/local/tmp/not-qualified",
                    "kind": "absent",
                    "sha256": None,
                    "size": None,
                    "mode": None,
                    "uid": None,
                    "gid": None,
                    "mtime_epoch": None,
                    "selinux_context": None,
                }
            )
            escaped["target"]["baseline_inventory"].sort(key=lambda item: item["path"])
            escaped["target"]["baseline_inventory_sha256"] = _inventory_digest(
                escaped["target"]["baseline_inventory"]
            )
            with self.assertRaisesRegex(ValueError, "escaped its allowlist"):
                validate_qualification_record(escaped)

            reused = copy.deepcopy(record)
            reused["persistence"]["exec_proofs"][2]["magisk_result"]["sha256"] = (
                reused["persistence"]["exec_proofs"][0]["magisk_result"]["sha256"]
            )
            with self.assertRaisesRegex(ValueError, "not fresh on every boot"):
                validate_qualification_record(reused)

    def test_record_rejects_duplicate_boots_and_inexact_restore(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-qualification-invalid-") as temp:
            record, _, _, _ = self.record(Path(temp))
            duplicate = copy.deepcopy(record)
            duplicate["persistence"]["cold_boot_ids"][2] = "a" * 64
            with self.assertRaises(ValueError):
                validate_qualification_record(duplicate)
            changed = copy.deepcopy(record)
            changed["recovery"]["target_contract_sha256_after_restore"] = "9" * 64
            with self.assertRaisesRegex(ValueError, "restored target contract"):
                validate_qualification_record(changed)
            missing_anchor = copy.deepcopy(record)
            missing_anchor["challenge"]["anchor_after_restore"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "pre-backup anchor"):
                validate_qualification_record(missing_anchor)
            reused_restore_boot = copy.deepcopy(record)
            reused_restore_boot["recovery"]["restored_boot_id_sha256"] = "a" * 64
            with self.assertRaisesRegex(ValueError, "distinct boot ID"):
                validate_qualification_record(reused_restore_boot)
            escaped_probe = copy.deepcopy(record)
            escaped_probe["init"]["probe"]["path"] = (
                "/system/etc/init/../kitsune-system-mode-qualification-"
                f"{record['record_id']}.rc"
            )
            with self.assertRaisesRegex(ValueError, "record identity"):
                validate_qualification_record(escaped_probe)
            duplicate_backup_argument = copy.deepcopy(record)
            duplicate_backup_argument["commands"]["backup"]["argv"].append(
                "{backup}"
            )
            with self.assertRaisesRegex(ValueError, "exact one-path template"):
                validate_qualification_record(duplicate_backup_argument)

    def test_target_digest_ignores_boot_id_and_incidental_free_space_only(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = fixture_report(fixture["input"])
        changed = copy.deepcopy(report)
        changed["device"]["boot_id_sha256"] = "9" * 64
        changed["staging"]["available_bytes"] += 4096
        self.assertEqual(stable_target_digest(report), stable_target_digest(changed))
        changed["staging"]["sufficient"] = False
        self.assertNotEqual(stable_target_digest(report), stable_target_digest(changed))

    def test_report_cannot_replace_qualified_recovery_or_persistence_evidence(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = fixture_report(fixture["input"])
        with tempfile.TemporaryDirectory(prefix="kitsune-qualification-binding-") as temp:
            root = Path(temp)
            record, record_path, _, _ = self.record(root)
            report["source"]["serial_sha256"] = record["target"]["serial_sha256"]
            report["source"]["repository_commit"] = record["source"]["repository_commit"]
            report["source"]["probe_sha256"] = record["source"]["probe_sha256"]
            report["device"]["fingerprint_sha256"] = record["target"]["fingerprint_sha256"]
            report["device"]["api"] = record["target"]["api"]
            report["device"]["abis"] = record["target"]["abis"]
            contract = stable_target_digest(report)
            record["target"]["baseline_contract_sha256"] = contract
            record["recovery"]["target_contract_sha256_after_restore"] = contract
            _seal_record(record, Path(os.environ[QUALIFICATION_KEY_ENV]))
            record_path.write_bytes(_canonical_bytes(record))

            evidence = evidence_from_record(record_path)
            report["recovery"] = {
                "snapshot_id": evidence.snapshot_id,
                "backup_location": evidence.backup_location,
                "backup_digest": evidence.backup_digest,
                "restore_command": evidence.restore_command,
                "qualification_record": evidence.qualification_record,
                "qualification_sha256": evidence.qualification_sha256,
                "adapter_id": evidence.adapter_id,
                "instance_identity_sha256": evidence.instance_identity_sha256,
                "verified": True,
            }
            report["persistence"] = {
                "backing_write_probe": evidence.backing_write_probe,
                "cold_boots": evidence.cold_boots,
                "host_restarts": evidence.host_restarts,
                "proven": True,
            }
            report["assessment"] = classify_report(report)
            self.assertEqual(record, verify_report_qualification(report))

            with mock.patch(
                "tools.system_mode.authorization.digest_backup_path",
                wraps=digest_backup_path,
            ) as digest_backup:
                loaded, loaded_evidence = load_qualification_evidence(record_path)
                self.assertEqual(
                    record,
                    verify_report_qualification_evidence(
                        report,
                        loaded,
                        loaded_evidence,
                    ),
                )
                verify_report_qualification_evidence(
                    report,
                    loaded,
                    loaded_evidence,
                )
                self.assertEqual(1, digest_backup.call_count)

            replaced_recovery = copy.deepcopy(report)
            replaced_recovery["recovery"]["restore_command"] = "/opt/mumu restore another-vm"
            with self.assertRaisesRegex(ValueError, "recovery evidence differs"):
                verify_report_qualification(replaced_recovery)

            replaced_persistence = copy.deepcopy(report)
            replaced_persistence["persistence"]["cold_boots"] += 1
            with self.assertRaisesRegex(ValueError, "persistence evidence differs"):
                verify_report_qualification(replaced_persistence)

    def test_qualification_executes_three_boots_and_the_restore(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        baseline = fixture_report(fixture["input"])
        baseline["init"]["import_proof"] = "unproven"
        baseline["recovery"]["verified"] = False
        baseline["persistence"]["proven"] = False
        baseline["assessment"] = classify_report(baseline)
        after_backup = copy.deepcopy(baseline)
        restored = copy.deepcopy(baseline)

        with tempfile.TemporaryDirectory(prefix="kitsune-qualification-runner-") as temp:
            root = Path(temp).resolve()
            backup = root / "backup.img"
            identity_path = root / "instance.json"
            identity_path.write_text('{"vm":0}\n', encoding="ascii")
            output = root / "qualification.json"
            wrappers = {
                name: self.executable(root, f"{name}.sh")
                for name in ("backup", "cold-boot", "restore")
            }
            boots = [str(uuid.UUID(int=value)) for value in range(2, 9)]
            host_commands: list[tuple[str, Path | None]] = []
            remote: dict[str, dict[str, object]] = {}
            anchors_before: dict[str, dict[str, object]] = {}
            inventory = [
                {
                    "path": path,
                    "kind": "absent",
                    "sha256": None,
                    "size": None,
                    "mode": None,
                    "uid": None,
                    "gid": None,
                    "mtime_epoch": None,
                    "selinux_context": None,
                }
                for path in _boot_critical_roots("/system/etc/init")
            ]

            def evidence(path: str, content: bytes, mode: str) -> dict[str, object]:
                return {
                    "path": path,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "mode": mode,
                    "uid": 0,
                    "gid": 0,
                    "selinux_context": "u:object_r:system_file:s0",
                }

            def write_probe(
                _client: object,
                path: str,
                content: bytes,
                mode: str = "0644",
                selinux_context: str | None = "u:object_r:system_file:s0",
            ) -> dict[str, object]:
                item = evidence(path, content, mode)
                if selinux_context is None:
                    item["selinux_context"] = "u:object_r:adb_data_file:s0"
                remote[path] = item
                if "backup-anchor" in path:
                    anchors_before[path] = dict(item)
                return item

            def replace_probe(
                _client: object,
                path: str,
                content: bytes,
                mode: str = "0600",
                selinux_context: str | None = "u:object_r:system_file:s0",
            ) -> dict[str, object]:
                item = evidence(path, content, mode)
                if selinux_context is None:
                    item["selinux_context"] = "u:object_r:adb_data_file:s0"
                remote[path] = item
                return item

            def run_host(
                _command: dict[str, object],
                purpose: str,
                _timeout: int,
                *,
                backup_location: Path | None = None,
            ) -> None:
                host_commands.append((purpose, backup_location))
                if purpose == "challenge backup creation":
                    assert backup_location is not None
                    backup_location.write_bytes(b"challenged-baseline")
                if purpose == "clean backup creation":
                    assert backup_location == backup
                    backup.write_bytes(b"clean-baseline")
                if purpose == "challenge restore":
                    for path, item in anchors_before.items():
                        remote[path] = dict(item)

            def remote_evidence(_client: object, path: str) -> dict[str, object]:
                return dict(remote[path])

            proof_counter = iter(range(1, 4))

            def exec_proof(
                _client: object,
                *,
                result_paths: dict[str, str],
                boot_id: str,
                **_kwargs: object,
            ) -> dict[str, object]:
                value = next(proof_counter)
                return {
                    "boot_id_sha256": hashlib.sha256(boot_id.encode()).hexdigest(),
                    "init_result": evidence(
                        result_paths["init"],
                        f"init-{value}".encode(),
                        "0600",
                    ),
                    "magisk_result": evidence(
                        result_paths["magisk"],
                        f"magisk-{value}".encode(),
                        "0600",
                    ),
                }

            with (
                mock.patch("tools.system_mode.qualification._policy_from_apk",
                           return_value=("d" * 64, b"candidate-policy")),
                mock.patch(
                    "tools.system_mode.qualification.collect_report",
                    side_effect=[
                        baseline,
                        after_backup,
                        copy.deepcopy(baseline),
                        copy.deepcopy(baseline),
                        restored,
                    ],
                ),
                mock.patch(
                    "tools.system_mode.qualification._boot_id",
                    return_value=str(uuid.UUID(int=1)),
                ),
                mock.patch(
                    "tools.system_mode.qualification._wait_for_new_boot",
                    side_effect=boots,
                ),
                mock.patch(
                    "tools.system_mode.qualification._write_probe",
                    side_effect=write_probe,
                ),
                mock.patch(
                    "tools.system_mode.qualification._replace_probe",
                    side_effect=replace_probe,
                ),
                mock.patch(
                    "tools.system_mode.qualification._remote_file_evidence",
                    side_effect=remote_evidence,
                ),
                mock.patch(
                    "tools.system_mode.qualification._verify_init_exec_probe",
                    side_effect=exec_proof,
                ),
                mock.patch(
                    "tools.system_mode.qualification._boot_critical_inventory",
                    return_value=inventory,
                ),
                mock.patch(
                    "tools.system_mode.qualification._root_checked",
                    return_value="absent",
                ),
                mock.patch(
                    "tools.system_mode.qualification._run_host",
                    side_effect=run_host,
                ),
            ):
                record = qualify_target(
                    mock.Mock(),
                    output=output,
                    backup_location=backup,
                    snapshot_id="baseline-vm0",
                    backup_command=(
                        f"{shlex.quote(str(wrappers['backup']))} '{{backup}}'"
                    ),
                    restore_command=(
                        f"{shlex.quote(str(wrappers['restore']))} '{{backup}}'"
                    ),
                    cold_boot_command=str(wrappers["cold-boot"]),
                    instance_identity_path=identity_path,
                    seal_key_path=root / "seal.key",
                    endpoint="127.0.0.1:16384",
                    lifecycle_timeout=30,
                    artifact_path=root / "candidate.apk",
                )

            self.assertEqual("d" * 64, record["init"]["artifact_sha256"])
            self.assertEqual(_sha256(b"candidate-policy"), record["init"]["policy"]["sha256"])
            self.assertTrue(output.is_file())
            self.assertEqual(3, len(record["persistence"]["cold_boot_ids"]))
            self.assertEqual(
                [
                    "challenge backup creation",
                    "cold boot",
                    "cold boot",
                    "cold boot",
                    "challenge restore",
                    "clean backup creation",
                    "clean backup restore",
                ],
                [purpose for purpose, _ in host_commands],
            )
            self.assertTrue(record["recovery"]["restore_executed"])
            self.assertEqual(
                hashlib.sha256(boots[-1].encode("utf-8")).hexdigest(),
                record["recovery"]["restored_boot_id_sha256"],
            )
            self.assertEqual(
                record["challenge"]["anchor_before"],
                record["challenge"]["anchor_after_restore"],
            )
            self.assertFalse(any(root.glob(".*kitsune-challenge-*")))
            self.assertEqual("absent", record["backup"]["qualification_residue"])
            record["init"].pop("artifact_sha256")
            with self.assertRaisesRegex(ValueError, "present together"):
                validate_qualification_record(record)


if __name__ == "__main__":
    unittest.main()
