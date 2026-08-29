"""Complete backup coverage, immutable manifests, and hostile restore cases."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from scripts.sqlite_snapshot import snapshot
from scripts.verify_backup import restore_backup


REPO_ROOT = Path(__file__).parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup.sh"
REQUIRED_DATABASES = ("journal", "users", "evaluations", "postmarket_shadow")
ALL_DATABASES = (*REQUIRED_DATABASES, "universe")


def _database(path: Path, marker: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE durable_state (value TEXT NOT NULL)")
        conn.execute("INSERT INTO durable_state VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _fixture_data(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    for name in ALL_DATABASES:
        _database(data / f"{name}.db", f"{name}-marker")
    audits = data / "postmarket_audits"
    evidence = data / "postmarket_evidence" / "17afffd" / "controls"
    audits.mkdir()
    evidence.mkdir(parents=True)
    (audits / "postmarket_audit_2026-08-27_v1.json").write_text(
        '{"operational_clean":true}\n', encoding="utf-8"
    )
    (audits / "postmarket_rank_empirical_deadbeef_holdout_cafebabe_v1.json").write_text(
        '{"artifact_type":"postmarket_rank_empirical"}\n', encoding="utf-8"
    )
    (evidence / "kill_switch.json").write_text(
        '{"status":"passed"}\n', encoding="utf-8"
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=not-a-real-secret\n", encoding="utf-8")
    return data, env_file


def _run_backup(
    tmp_path: Path,
    *,
    data: Path | None = None,
    env_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if data is None or env_file is None:
        data, env_file = _fixture_data(tmp_path)
    backup_dir = tmp_path / "backups"
    environment = {
        **os.environ,
        "DATA_DIR": str(data),
        "BACKUP_DIR": str(backup_dir),
        "ENV_FILE": str(env_file),
        "RETAIN_DAYS": "14",
    }
    environment.pop("RCLONE_REMOTE", None)
    environment.pop("BACKUP_ENCRYPTION_PASSPHRASE_FILE", None)
    environment.update(extra_env or {})
    result = subprocess.run(
        ["bash", str(BACKUP_SCRIPT)],
        cwd=REPO_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )
    return result, backup_dir


def _manifest(backup_dir: Path) -> Path:
    manifests = list(backup_dir.glob("manifest_*.sha256"))
    assert len(manifests) == 1
    return manifests[0]


def _rewrite_manifest_digest(manifest: Path, filename: str) -> None:
    path = manifest.parent / filename
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lines = manifest.read_text(encoding="utf-8").splitlines()
    manifest.write_text(
        "\n".join(
            f"{digest}  {filename}" if line.endswith(f"  {filename}") else line
            for line in lines
        )
        + "\n",
        encoding="utf-8",
    )


def test_sqlite_snapshot_is_consistent_and_never_overwrites(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "snapshot.db"
    _database(source, "evidence")

    snapshot(source, destination)

    conn = sqlite3.connect(destination)
    try:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT value FROM durable_state").fetchone() == ("evidence",)
    finally:
        conn.close()
    with pytest.raises(FileExistsError, match="refusing to replace"):
        snapshot(source, destination)


def test_sqlite_snapshot_rejects_source_and_destination_symlinks(tmp_path):
    source = tmp_path / "source.db"
    _database(source, "evidence")
    source_link = tmp_path / "source-link.db"
    source_link.symlink_to(source)

    with pytest.raises(ValueError, match="symlinked source"):
        snapshot(source_link, tmp_path / "snapshot.db")

    destination = tmp_path / "destination.db"
    destination.symlink_to(tmp_path / "missing.db")
    with pytest.raises(FileExistsError, match="refusing to replace"):
        snapshot(source, destination)


def test_complete_backup_set_restores_every_database_and_artifact(tmp_path):
    result, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)

    assert "files=6" in result.stdout
    assert {path.name.split("_2026", 1)[0] for path in backup_dir.glob("*.db.gz")} == set(
        ALL_DATABASES
    )
    restore_dir = tmp_path / "restore"
    report = restore_backup(manifest, restore_dir)

    assert set(report.databases) == set(ALL_DATABASES)
    assert len(report.verified_files) == 6
    for name in ALL_DATABASES:
        conn = sqlite3.connect(restore_dir / "data" / f"{name}.db")
        try:
            assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert conn.execute("SELECT value FROM durable_state").fetchone() == (
                f"{name}-marker",
            )
        finally:
            conn.close()
    assert (
        restore_dir / "data" / "postmarket_audits" / "postmarket_audit_2026-08-27_v1.json"
    ).is_file()
    assert (
        restore_dir
        / "data"
        / "postmarket_audits"
        / "postmarket_rank_empirical_deadbeef_holdout_cafebabe_v1.json"
    ).is_file()
    assert (
        restore_dir
        / "data"
        / "postmarket_evidence"
        / "17afffd"
        / "controls"
        / "kill_switch.json"
    ).is_file()
    assert json.loads((restore_dir / "restore_report.json").read_text())["stamp"]


def test_customer_dry_run_contracts_are_in_encrypted_artifact_set(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    contracts = {
        "postmarket_customer_delivery_policy.json": '{"policy":1}\n',
        "postmarket_customer_delivery_authorization.json": '{"authorization":1}\n',
        "postmarket_customer_dry_run_campaign.json": '{"campaign":1}\n',
    }
    for name, content in contracts.items():
        (data / name).write_text(content, encoding="utf-8")
    _, backup_dir = _run_backup(tmp_path, data=data, env_file=env_file)
    restore_dir = tmp_path / "restore"
    restore_backup(_manifest(backup_dir), restore_dir)
    for name, content in contracts.items():
        assert (restore_dir / "data" / name).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("missing", REQUIRED_DATABASES)
def test_missing_irreplaceable_database_fails_backup_loudly(tmp_path, missing):
    data, env_file = _fixture_data(tmp_path)
    (data / f"{missing}.db").unlink()

    result, backup_dir = _run_backup(
        tmp_path,
        data=data,
        env_file=env_file,
        check=False,
    )

    assert result.returncode != 0
    assert f"required database missing: {data / f'{missing}.db'}" in result.stderr
    assert not list(backup_dir.glob("manifest_*.sha256"))


def test_corrupt_required_database_fails_without_manifest(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    (data / "evaluations.db").write_text("not sqlite", encoding="utf-8")

    result, backup_dir = _run_backup(
        tmp_path,
        data=data,
        env_file=env_file,
        check=False,
    )

    assert result.returncode != 0
    assert not list(backup_dir.glob("manifest_*.sha256"))
    assert not list(backup_dir.glob("evaluations_*.db"))


def test_optional_universe_and_artifacts_can_be_absent_on_fresh_install(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    (data / "universe.db").unlink()
    for path in sorted(data.glob("postmarket_*"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)

    result, backup_dir = _run_backup(tmp_path, data=data, env_file=env_file)
    report = restore_backup(_manifest(backup_dir), tmp_path / "restore")

    assert "artifact archive skipped" in result.stdout
    assert set(report.databases) == set(REQUIRED_DATABASES)
    assert report.postmarket_artifacts == ()


def test_retention_deletes_only_recognized_backup_names(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    recognized = backup_dir / "journal_20000101T000000Z.db.gz"
    unrelated = backup_dir / "customer_export.db.gz"
    recognized.write_text("old", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(recognized, (old, old))
    os.utime(unrelated, (old, old))

    _run_backup(
        tmp_path,
        data=data,
        env_file=env_file,
        extra_env={"RETAIN_DAYS": "1"},
    )

    assert not recognized.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_tampered_backup_fails_before_restore_directory_is_created(tmp_path):
    _, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)
    journal = next(backup_dir.glob("journal_*.db.gz"))
    journal.write_bytes(journal.read_bytes() + b"tampered")
    destination = tmp_path / "restore"

    with pytest.raises(ValueError, match="digest mismatch"):
        restore_backup(manifest, destination)

    assert not destination.exists()


def test_restore_rejects_archive_path_traversal_without_partial_output(tmp_path):
    _, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)
    archive = next(backup_dir.glob("postmarket_artifacts_*.tar.gz"))
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"escape"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    _rewrite_manifest_digest(manifest, archive.name)
    destination = tmp_path / "restore"

    with pytest.raises(ValueError, match="unsafe artifact archive path"):
        restore_backup(manifest, destination)

    assert not destination.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_backup_rejects_symlinked_postmarket_artifact_before_manifest(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("outside\n", encoding="utf-8")
    (data / "postmarket_evidence" / "unsafe.json").symlink_to(target)

    result, backup_dir = _run_backup(
        tmp_path,
        data=data,
        env_file=env_file,
        check=False,
    )

    assert result.returncode != 0
    assert "artifact archive contains non-file entry" in result.stdout
    assert not list(backup_dir.glob("manifest_*.sha256"))


def test_manifest_cannot_omit_required_database_even_with_valid_digests(tmp_path):
    _, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)
    manifest.write_text(
        "\n".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if "postmarket_shadow_" not in line
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required databases"):
        restore_backup(manifest, tmp_path / "restore")


def test_restore_refuses_existing_or_broken_symlink_destination(tmp_path):
    _, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)
    destination = tmp_path / "restore"
    destination.symlink_to(tmp_path / "missing-target", target_is_directory=True)

    with pytest.raises(FileExistsError, match="refusing to replace"):
        restore_backup(manifest, destination)


def test_restore_rejects_symlinked_manifest_or_payload(tmp_path):
    _, backup_dir = _run_backup(tmp_path)
    manifest = _manifest(backup_dir)
    manifest_link = tmp_path / manifest.name
    manifest_link.symlink_to(manifest)

    with pytest.raises(ValueError, match="manifest must not be a symlink"):
        restore_backup(manifest_link, tmp_path / "restore-from-manifest-link")

    journal = next(backup_dir.glob("journal_*.db.gz"))
    moved = backup_dir / "journal-real.db.gz"
    journal.rename(moved)
    journal.symlink_to(moved)
    with pytest.raises(ValueError, match="manifest file must not be a symlink"):
        restore_backup(manifest, tmp_path / "restore-from-payload-link")


def test_encrypted_offbox_stage_includes_all_irreplaceable_state(tmp_path):
    data, env_file = _fixture_data(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gpg = fake_bin / "gpg"
    gpg.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
out=""
while (($#)); do
  if [[ "$1" == "-o" ]]; then out="$2"; shift 2; continue; fi
  src="$1"
  shift
done
cp "$src" "$out"
""",
        encoding="utf-8",
    )
    rclone = fake_bin / "rclone"
    rclone.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "copy" ]]; then
  for path in "$2"/*; do
    if [[ -f "$path" ]]; then
      basename "$path"
      cp "$path" "$FAKE_RCLONE_CAPTURE/"
    fi
  done | sort > "$FAKE_RCLONE_LOG"
fi
""",
        encoding="utf-8",
    )
    gpg.chmod(0o755)
    rclone.chmod(0o755)
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("not-a-real-passphrase\n", encoding="utf-8")
    log = tmp_path / "rclone-files.txt"
    capture = tmp_path / "remote"
    capture.mkdir()

    _run_backup(
        tmp_path,
        data=data,
        env_file=env_file,
        extra_env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RCLONE_REMOTE": "fake:backups",
            "RCLONE_CONFIG": str(tmp_path / "rclone.conf"),
            "BACKUP_ENCRYPTION_PASSPHRASE_FILE": str(passphrase),
            "FAKE_RCLONE_LOG": str(log),
            "FAKE_RCLONE_CAPTURE": str(capture),
        },
    )

    names = log.read_text(encoding="utf-8").splitlines()
    assert any(name.startswith("journal_") and name.endswith(".db.gz.gpg") for name in names)
    assert any(name.startswith("users_") and name.endswith(".db.gz.gpg") for name in names)
    assert any(
        name.startswith("evaluations_") and name.endswith(".db.gz.gpg") for name in names
    )
    assert any(
        name.startswith("postmarket_shadow_") and name.endswith(".db.gz.gpg")
        for name in names
    )
    assert any(name.startswith("postmarket_artifacts_") for name in names)
    assert any(name.startswith("manifest_") and name.endswith(".sha256.gpg") for name in names)
    assert any(name.startswith("env_") and name.endswith(".gpg") for name in names)
    assert not any(name.startswith("universe_") for name in names)

    # Fake gpg copies plaintext, so stripping the .gpg suffix simulates an
    # off-box download/decrypt. The remote manifest must restore the complete
    # irrebuildable set without asking for the intentionally omitted universe.
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    for encrypted in capture.glob("*.gpg"):
        if encrypted.name.startswith("env_"):
            continue
        shutil.copyfile(encrypted, downloaded / encrypted.name[:-4])
    remote_manifest = next(downloaded.glob("manifest_*.sha256"))
    report = restore_backup(remote_manifest, tmp_path / "offbox-restore")
    assert set(report.databases) == set(REQUIRED_DATABASES)
    assert "universe" not in report.databases
    assert report.postmarket_artifacts


def test_configured_offbox_without_key_fails_instead_of_downgrading(tmp_path):
    result, _ = _run_backup(
        tmp_path,
        extra_env={
            "RCLONE_REMOTE": "configured:remote",
            "BACKUP_ENCRYPTION_PASSPHRASE_FILE": str(tmp_path / "missing-key"),
        },
        check=False,
    )

    assert result.returncode != 0
    assert "refusing to downgrade to local-only backup" in result.stderr


@pytest.mark.parametrize("remote", ["missing-colon", "remote:"])
def test_offbox_remote_root_or_malformed_remote_is_rejected(tmp_path, remote):
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("not-a-real-passphrase\n", encoding="utf-8")

    result, _ = _run_backup(
        tmp_path,
        extra_env={
            "RCLONE_REMOTE": remote,
            "BACKUP_ENCRYPTION_PASSPHRASE_FILE": str(passphrase),
        },
        check=False,
    )

    assert result.returncode != 0
    assert "refusing remote-root access" in result.stderr


@pytest.mark.parametrize("retain_days", ["-1", "1 day", "$(touch nope)"])
def test_invalid_retention_configuration_fails_before_backup(tmp_path, retain_days):
    result, backup_dir = _run_backup(
        tmp_path,
        extra_env={"RETAIN_DAYS": retain_days},
        check=False,
    )

    assert result.returncode != 0
    assert "RETAIN_DAYS must be a non-negative integer" in result.stderr
    assert not list(backup_dir.glob("manifest_*.sha256"))
