"""Schema status and guarded migration (single runner, pre-migration backup, integrity verification). Migrations never drop or recreate the database."""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import BACKEND_DIR

LOCK_STALE_SECONDS = 3600


class MigrationError(RuntimeError):
    pass


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head() or ""


def revisions_in_order() -> list[str]:
    sd = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    return [r.revision for r in reversed(list(sd.walk_revisions()))]


def schema_status(url: str) -> dict[str, Any]:
    """state: missing (no database / no version table), behind, at_head, ahead_or_unknown (database revision unknown to this code)."""
    head = head_revision()
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:" and not Path(parsed.database).exists():
        return {"state": "missing", "current": None, "head": head, "at_head": False}
    engine = create_engine(url, connect_args={"timeout": 5} if parsed.get_backend_name() == "sqlite" else {})
    try:
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()
    if current is None:
        state = "missing"
    elif current == head:
        state = "at_head"
    elif current in revisions_in_order():
        state = "behind"
    else:
        state = "ahead_or_unknown"
    return {"state": state, "current": current, "head": head, "at_head": state == "at_head"}


@contextmanager
def migration_lock(url: str, *, break_stale: bool = False) -> Iterator[Path]:
    """Cross-process exclusive lock so only one runner migrates at a time (replicas must not each run migrations)."""
    parsed = make_url(url)
    base = Path(parsed.database) if parsed.get_backend_name() == "sqlite" and parsed.database else BACKEND_DIR / "data" / "nova"
    lock = base.with_suffix(base.suffix + ".migrate.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - lock.stat().st_mtime
        if break_stale and age > LOCK_STALE_SECONDS:
            lock.unlink()
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        else:
            raise MigrationError(f"Another migration appears to be running (lock {lock.name}, {int(age)}s old). If you are sure none is running and the lock is older than {LOCK_STALE_SECONDS}s, re-run with --break-stale-lock.") from None
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
        os.close(fd)
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def integrity_check(url: str) -> dict[str, Any]:
    """SQLite: PRAGMA integrity_check and foreign_key_check. Other backends are not supported by this tooling."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return {"supported": False, "ok": None, "detail": "integrity tooling supports SQLite only"}
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            integrity = [r[0] for r in conn.execute(text("PRAGMA integrity_check"))]
            fk = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
    finally:
        engine.dispose()
    return {"supported": True, "ok": integrity == ["ok"] and not fk, "integrity": integrity[:5], "foreign_key_violations": len(fk)}


def row_counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))]
            return {t: int(conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() or 0) for t in tables}  # noqa: S608 - names come from sqlite_master
    finally:
        engine.dispose()


def migrate(url: str, *, backup_dir: Path | None = None, break_stale_lock: bool = False) -> dict[str, Any]:
    """Upgrade to head under a lock. If the database already holds data and a backup dir is given, a verified backup is taken first.
    Never downgrades, never drops the database. Raises MigrationError with a clear reason."""
    before = schema_status(url)
    if before["state"] == "ahead_or_unknown":
        raise MigrationError(f"Database revision {before['current']} is unknown to this release (head {before['head']}). Refusing to touch it: deploy a release that includes that revision, or restore a compatible backup.")
    if before["at_head"]:
        return {"changed": False, "before": before["current"], "after": before["current"], "backup": None}
    backup = None
    with migration_lock(url, break_stale=break_stale_lock):
        if before["state"] == "behind" and backup_dir is not None:
            from app.release import backup as bk
            backup = bk.create_backup(url, backup_dir, artifact_root=None, label="pre-migration")["path"]
        counts_before = row_counts(url) if before["state"] == "behind" else {}
        try:
            command.upgrade(alembic_config(url), "head")
        except Exception as exc:
            raise MigrationError(f"Migration failed ({type(exc).__name__}); the database was not recreated. {'A pre-migration backup is at ' + str(backup) + '. ' if backup else ''}Diagnose with `python -m app.release status`.") from None
        after = schema_status(url)
        if not after["at_head"]:
            raise MigrationError("Migration finished but the schema is not at head.")
        check = integrity_check(url)
        if check.get("ok") is False:
            raise MigrationError("Post-migration integrity check failed; restore the pre-migration backup." + (f" Backup: {backup}" if backup else ""))
        counts_after = row_counts(url)
        lost = {t: n - counts_after.get(t, 0) for t, n in counts_before.items() if counts_after.get(t, 0) < n}
        if lost:
            raise MigrationError(f"Migration reduced row counts in {sorted(lost)}; restore the pre-migration backup." + (f" Backup: {backup}" if backup else ""))
    return {"changed": True, "before": before["current"], "after": after["current"], "backup": str(backup) if backup else None}


def classify_migrations() -> list[dict[str, Any]]:
    """Heuristic expand-and-contract classification by scanning each migration's upgrade operations. 'additive' means only new tables / indexes /
    nullable-or-defaulted columns: the previous release's code keeps working against that schema. Anything else needs a human decision."""
    sd = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    out = []
    risky = ("drop_table", "drop_column", "alter_column", "rename_table", "execute(", "drop_constraint", "drop_index")
    for rev in reversed(list(sd.walk_revisions())):
        src = Path(rev.path).read_text(encoding="utf-8")
        up = src.split("def upgrade", 1)[1].split("def downgrade", 1)[0] if "def upgrade" in src else ""
        found = sorted({k.rstrip("(") for k in risky if k in up})
        for m in re.finditer(r"add_column\((.*?)\)\s*$", up, re.S | re.M):  # a NOT NULL column without a server default breaks the previous release's INSERTs
            if "nullable=False" in m.group(1) and "server_default" not in m.group(1):
                found.append("add_column_not_null_without_default")
                break
        found = sorted(set(found))
        out.append({"revision": rev.revision, "down_revision": rev.down_revision, "additive": not found, "needs_review": found})
    return out
