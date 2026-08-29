#!/usr/bin/env python3
"""Verify a Perch backup manifest and restore it into an isolated directory."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradebot.screening_archive import verify_screening_archive


MANIFEST_PATTERN = re.compile(r"^manifest_(\d{8}T\d{6}Z)\.sha256$")
DIGEST_LINE_PATTERN = re.compile(r"^([0-9a-f]{64}) [ *]([A-Za-z0-9_.-]+)$")
DATABASES = {"journal", "users", "universe", "evaluations", "postmarket_shadow"}
# Legacy off-box sets created before universe.db gained mandatory custody did
# not include it. Keep isolated disaster restore backward-compatible; current
# backup generation and signal-quality preflight separately require universe.db.
REQUIRED_DATABASES = {"journal", "users", "evaluations", "postmarket_shadow"}
ARTIFACT_ROOTS = {"postmarket_audits", "postmarket_evidence", "screening_archives"}
ARTIFACT_FILES = {
    "postmarket_customer_delivery_policy.json",
    "postmarket_customer_delivery_authorization.json",
    "postmarket_customer_dry_run_campaign.json",
}


@dataclass(frozen=True)
class VerifiedFile:
    name: str
    sha256: str
    kind: str


@dataclass(frozen=True)
class RestoreReport:
    manifest: str
    manifest_sha256: str
    stamp: str
    databases: tuple[str, ...]
    postmarket_artifacts: tuple[str, ...]
    verified_files: tuple[VerifiedFile, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_manifest(path: Path) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    match = MANIFEST_PATTERN.fullmatch(path.name)
    if not match:
        raise ValueError("manifest filename must be manifest_<UTC stamp>.sha256")
    stamp = match.group(1)
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    database_names: set[str] = set()
    artifact_seen = False
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line_match = DIGEST_LINE_PATTERN.fullmatch(line)
        if not line_match:
            raise ValueError(f"invalid manifest line {line_number}")
        digest, name = line_match.groups()
        if name in seen:
            raise ValueError(f"duplicate manifest filename {name!r}")
        seen.add(name)
        db_match = re.fullmatch(
            rf"(journal|users|universe|evaluations|postmarket_shadow)_{stamp}\.db\.gz",
            name,
        )
        if db_match:
            database = db_match.group(1)
            database_names.add(database)
            rows.append((name, digest, database))
            continue
        if name == f"postmarket_artifacts_{stamp}.tar.gz":
            if artifact_seen:
                raise ValueError("duplicate postmarket artifact archive")
            artifact_seen = True
            rows.append((name, digest, "postmarket_artifacts"))
            continue
        raise ValueError(f"manifest contains unsupported or mismatched filename {name!r}")
    missing = REQUIRED_DATABASES - database_names
    if missing:
        raise ValueError(f"manifest is missing required databases: {sorted(missing)}")
    return stamp, tuple(rows)


def _verify_files(
    backup_dir: Path,
    rows: tuple[tuple[str, str, str], ...],
) -> tuple[VerifiedFile, ...]:
    verified = []
    for name, expected, kind in rows:
        path = backup_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"manifest file is missing: {name}")
        if path.is_symlink():
            raise ValueError(f"manifest file must not be a symlink: {name}")
        observed = _sha256(path)
        if observed != expected:
            raise ValueError(
                f"digest mismatch for {name}: expected {expected}, observed {observed}"
            )
        verified.append(VerifiedFile(name=name, sha256=observed, kind=kind))
    return tuple(verified)


def _restore_database(archive: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    uri = f"{destination.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        result = [row[0] for row in conn.execute("PRAGMA quick_check")]
    finally:
        conn.close()
    if result != ["ok"]:
        raise sqlite3.DatabaseError(
            f"restored database quick_check failed for {destination.name}: {result!r}"
        )


def _safe_artifact_members(archive: tarfile.TarFile) -> tuple[tarfile.TarInfo, ...]:
    safe = []
    seen: set[PurePosixPath] = set()
    for member in archive.getmembers():
        member_path = PurePosixPath(member.name)
        if member_path.is_absolute() or ".." in member_path.parts:
            raise ValueError(f"unsafe artifact archive path {member.name!r}")
        if not member_path.parts or (
            member_path.parts[0] not in ARTIFACT_ROOTS
            and member_path.as_posix() not in ARTIFACT_FILES
        ):
            raise ValueError(f"unexpected artifact archive root {member.name!r}")
        if member_path in seen:
            raise ValueError(f"duplicate artifact archive path {member.name!r}")
        seen.add(member_path)
        if not (member.isdir() or member.isfile()):
            raise ValueError(f"artifact archive contains non-file entry {member.name!r}")
        safe.append(member)
    return tuple(safe)


def _restore_artifacts(archive_path: Path, data_dir: Path) -> tuple[str, ...]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = _safe_artifact_members(archive)
        for member in members:
            relative = PurePosixPath(member.name)
            if (
                member.isfile()
                and relative.parts[0] == "screening_archives"
                and len(relative.parts) != 2
            ):
                raise ValueError(
                    f"screening archive must be directly beneath its root: {member.name!r}"
                )
            destination = data_dir.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise tarfile.ExtractError(f"cannot read artifact {member.name!r}")
            with source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            if relative.parts[0] == "screening_archives":
                verify_screening_archive(destination)
    return tuple(sorted(member.name for member in members if member.isfile()))


def _validate_screening_members(
    archive: tarfile.TarFile,
    members: tuple[tarfile.TarInfo, ...],
) -> None:
    screening = [
        member
        for member in members
        if member.isfile() and PurePosixPath(member.name).parts[0] == "screening_archives"
    ]
    if not screening:
        return
    with tempfile.TemporaryDirectory(prefix="perch-screening-archive-check.") as directory:
        root = Path(directory)
        for member in screening:
            relative = PurePosixPath(member.name)
            if len(relative.parts) != 2:
                raise ValueError(
                    f"screening archive must be directly beneath its root: {member.name!r}"
                )
            source = archive.extractfile(member)
            if source is None:
                raise tarfile.ExtractError(f"cannot read artifact {member.name!r}")
            destination = root / relative.name
            with source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
            verify_screening_archive(destination)


def validate_artifact_archive(archive_path: Path) -> tuple[str, ...]:
    with tarfile.open(archive_path, "r:gz") as archive:
        members = _safe_artifact_members(archive)
        _validate_screening_members(archive, members)
    return tuple(sorted(member.name for member in members if member.isfile()))


def restore_backup(manifest: Path, destination: Path) -> RestoreReport:
    if not manifest.is_file():
        raise FileNotFoundError(f"manifest does not exist: {manifest}")
    if manifest.is_symlink():
        raise ValueError("manifest must not be a symlink")
    if os.path.lexists(destination):
        raise FileExistsError(f"refusing to replace restore destination: {destination}")
    stamp, rows = _parse_manifest(manifest)
    verified = _verify_files(manifest.parent, rows)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    )
    try:
        data_dir = staging / "data"
        data_dir.mkdir()
        restored_databases = []
        restored_artifacts: tuple[str, ...] = ()
        for name, _, kind in rows:
            source = manifest.parent / name
            if kind in DATABASES:
                _restore_database(source, data_dir / f"{kind}.db")
                restored_databases.append(kind)
            elif kind == "postmarket_artifacts":
                restored_artifacts = _restore_artifacts(source, data_dir)
        report = RestoreReport(
            manifest=manifest.name,
            manifest_sha256=_sha256(manifest),
            stamp=stamp,
            databases=tuple(sorted(restored_databases)),
            postmarket_artifacts=restored_artifacts,
            verified_files=verified,
        )
        report_path = staging / "restore_report.json"
        with report_path.open("x", encoding="utf-8") as handle:
            json.dump(asdict(report), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--check-artifact-archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.check_artifact_archive is not None:
            if args.manifest is not None or args.destination is not None:
                parser.error("archive validation cannot be combined with restore arguments")
            files = validate_artifact_archive(args.check_artifact_archive)
            print(json.dumps({"archive": str(args.check_artifact_archive), "files": files}))
            return 0
        if args.manifest is None or args.destination is None:
            parser.error("manifest and destination are required for restore")
        report = restore_backup(args.manifest, args.destination)
    except (OSError, ValueError, sqlite3.DatabaseError, tarfile.TarError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
