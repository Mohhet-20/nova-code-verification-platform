"""Database-aware backup, verification, isolated restore and retention for SQLite deployments.

Data classes: DB (backed up), artifact files (backed up), encrypted credentials (inside the DB; the encryption KEY is deliberately NOT backed up and must be
stored separately), indexes/caches (derived, rebuildable), logs (not backed up). A backup is built in a ``.partial`` directory and renamed only when complete."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import APP_VERSION, BACKEND_DIR
from app.release import dbtools

FORMAT = 1
PREFIX = "nova-backup-"
# (table, column, kind): DB columns holding paths relative to the artifact root.
FILE_REFERENCES = (("test_generation_runs", "artifact_dir", "dir"), ("test_execution_runs", "artifact_dir", "dir"), ("test_candidates", "staged_path", "file"))


class BackupError(RuntimeError):
    pass


def sqlite_path(url: str) -> Path:
    u = make_url(url)
    if u.get_backend_name() != "sqlite" or not u.database or u.database == ":memory:":
        raise BackupError("Backup and restore support file-based SQLite databases only.")
    return Path(u.database)


def default_artifact_root(artifact_dir: str = "") -> Path:
    return Path(artifact_dir).expanduser() if artifact_dir else BACKEND_DIR / "data" / "artifacts"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _chmod(p: Path, mode: int) -> None:
    try:
        os.chmod(p, mode)
    except OSError:
        pass  # not supported on this filesystem (e.g. some Windows mounts)


def _artifact_files(root: Path | None) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and "-tmp" not in p.relative_to(root).parts[0])


def _artifact_empty_dirs(root: Path | None) -> list[Path]:
    """Empty directories are data too: a run that was refused or produced no output still records its artifact directory in the database,
    and the restore's file-reference check requires it to exist."""
    if root is None or not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink() and "-tmp" not in p.relative_to(root).parts[0]
                  and not any(p.iterdir()))


def create_backup(url: str, dest_root: Path, *, artifact_root: Path | None, label: str = "") -> dict[str, Any]:
    src = sqlite_path(url)
    if not src.exists():
        raise BackupError("Database file does not exist; nothing to back up.")
    dest_root.mkdir(parents=True, exist_ok=True)
    _chmod(dest_root, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{PREFIX}{stamp}" + (f"-{label}" if label else "")
    partial, final = dest_root / f".partial-{name}", dest_root / name
    if final.exists():
        raise BackupError(f"Backup {name} already exists.")
    partial.mkdir()
    try:
        db_copy = partial / "db.sqlite3"
        con, out = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30), sqlite3.connect(db_copy)
        try:
            con.backup(out)  # consistent snapshot even while the API is writing
        finally:
            out.close()
            con.close()
        files = []
        for f in _artifact_files(artifact_root):
            rel = f.relative_to(artifact_root).as_posix()
            target = partial / "artifacts" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            files.append({"path": rel, "sha256": sha256_file(target), "size": target.stat().st_size})
        empty_dirs = []
        for d in _artifact_empty_dirs(artifact_root):
            rel = d.relative_to(artifact_root).as_posix()
            (partial / "artifacts" / rel).mkdir(parents=True, exist_ok=True)
            empty_dirs.append(rel)
        snap_url = f"sqlite:///{db_copy.as_posix()}"
        status = dbtools.schema_status(snap_url)
        manifest = {
            "format": FORMAT, "created_at": datetime.now(UTC).isoformat(), "label": label, "app_version": APP_VERSION,
            "source_revision": os.environ.get("NOVA_SOURCE_REVISION", "unknown"), "schema_revision": status["current"], "code_head": status["head"],
            "db": {"file": "db.sqlite3", "sha256": sha256_file(db_copy), "size": db_copy.stat().st_size},
            "row_counts": dbtools.row_counts(snap_url), "artifacts": files, "artifact_dirs": empty_dirs,
            "not_included": ["integration encryption key (store separately)", "logs", "derived indexes are rebuildable and are included only as part of the DB",],
        }
        (partial / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        for p in [db_copy, partial / "manifest.json", *(partial / "artifacts").rglob("*")]:
            _chmod(p, 0o700 if p.is_dir() else 0o600)
        verify_dir(partial)  # a backup that cannot be verified is not a backup
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return {"path": final, "name": name, "manifest": manifest}


def verify_dir(path: Path) -> dict[str, Any]:
    mf = path / "manifest.json"
    if not mf.is_file():
        raise BackupError("manifest.json is missing (interrupted or foreign directory).")
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except ValueError:
        raise BackupError("manifest.json is not valid JSON.") from None
    if m.get("format") != FORMAT:
        raise BackupError(f"Unsupported backup format {m.get('format')!r}.")
    db = path / m["db"]["file"]
    if not db.is_file() or sha256_file(db) != m["db"]["sha256"]:
        raise BackupError("Database snapshot is missing or its checksum does not match.")
    for a in m["artifacts"]:
        f = path / "artifacts" / a["path"]
        if not f.is_file() or sha256_file(f) != a["sha256"]:
            raise BackupError(f"Artifact file {a['path']} is missing or corrupt.")
    for d in m.get("artifact_dirs", []):  # absent in backups made before 0.19.0
        if not (path / "artifacts" / d).is_dir():
            raise BackupError(f"Artifact directory {d} is missing.")
    integ = dbtools.integrity_check(f"sqlite:///{db.as_posix()}")
    if integ.get("integrity") != ["ok"]:  # structural corruption blocks a backup; FK orphans are recorded, not hidden (see restore report)
        raise BackupError("Database snapshot failed the SQLite integrity check.")
    return m


def list_backups(root: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(root.glob(f"{PREFIX}*")) if root.is_dir() else []:
        try:
            m = verify_dir(p)
            out.append({"name": p.name, "valid": True, "created_at": m["created_at"], "schema_revision": m["schema_revision"], "app_version": m["app_version"]})
        except BackupError as exc:
            out.append({"name": p.name, "valid": False, "error": str(exc)})
    out += [{"name": p.name, "valid": False, "error": "interrupted partial backup (safe to delete manually)"} for p in sorted(root.glob(".partial-*"))] if root.is_dir() else []
    return out


def file_reference_check(url: str, artifact_root: Path) -> dict[str, Any]:
    engine = create_engine(url)
    missing: list[str] = []
    checked = 0
    try:
        present = set(inspect(engine).get_table_names())
        absent = sorted({t for t, _, _ in FILE_REFERENCES} - present)
        if absent:  # unmigrated or foreign database: report it instead of failing with a traceback
            return {"checked": 0, "missing": [], "missing_count": 0, "ok": False, "tables_missing": absent,
                    "reason": "The database schema is missing tables that hold file references; run `python -m app.release status`."}
        with engine.connect() as conn:
            for table, col, kind in FILE_REFERENCES:
                for (val,) in conn.execute(text(f'SELECT "{col}" FROM "{table}" WHERE "{col}" != \'\'')):  # noqa: S608 - constants
                    checked += 1
                    p = (artifact_root / val).resolve()
                    ok = artifact_root.resolve() in p.parents and (p.is_dir() if kind == "dir" else p.is_file())
                    if not ok:
                        missing.append(f"{table}.{col}={val}")
    finally:
        engine.dispose()
    return {"checked": checked, "missing": missing[:50], "missing_count": len(missing), "ok": not missing}


def restore(backup: Path, target: Path, *, run_migrations: bool = True) -> dict[str, Any]:
    """Restore into an EMPTY separate target directory (db.sqlite3 + artifacts/). Never touches the active database: there is no overwrite option."""
    m = verify_dir(backup)
    if target.exists() and any(target.iterdir()):
        raise BackupError("Restore target must not exist or must be empty; restores never overwrite existing data.")
    target.mkdir(parents=True, exist_ok=True)
    _chmod(target, 0o700)
    shutil.copy2(backup / m["db"]["file"], target / "db.sqlite3")
    if (backup / "artifacts").is_dir():
        shutil.copytree(backup / "artifacts", target / "artifacts")
    url = f"sqlite:///{(target / 'db.sqlite3').as_posix()}"
    migrated = dbtools.migrate(url)["changed"] if run_migrations else False
    integ = dbtools.integrity_check(url)
    refs = file_reference_check(url, target / "artifacts")
    counts = dbtools.row_counts(url)
    shrunk = {t: n for t, n in m["row_counts"].items() if counts.get(t, 0) < n}
    ok = bool(integ.get("ok")) and refs["ok"] and not shrunk
    return {"ok": ok, "target": str(target), "database_url": url, "migrated": migrated, "integrity": integ, "file_references": refs, "row_counts_ok": not shrunk, "schema_revision": dbtools.schema_status(url)["current"]}


def prune(root: Path, *, keep: int) -> dict[str, Any]:
    """Delete the oldest VALID backups beyond ``keep``. Never deletes invalid or partial directories and never the last valid backup."""
    if keep < 1:
        raise BackupError("--keep must be at least 1.")
    valid = [b for b in list_backups(root) if b["valid"]]
    removable = valid[:-keep] if len(valid) > keep else []
    if len(valid) - len(removable) < 1:
        removable = []
    for b in removable:
        shutil.rmtree(root / b["name"])
    return {"kept": [b["name"] for b in valid if b not in removable], "removed": [b["name"] for b in removable]}
