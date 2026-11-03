# ruff: noqa: E702  (compact one-line command dispatch)
"""``python -m app.release <command>`` - operational CLI (cross-platform). Read-only commands: check-config, status, classify, verify, list-backups, integrity,
manifest, smoke, history, rollback-plan. Mutating commands: migrate, backup, restore (into a NEW directory only), prune, deploy. Secrets are never printed."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_DIR, get_settings
from app.release import backup as bk
from app.release import dbtools, deploy, manifest, prodcheck, smoke

# Both default inside the code tree for local use; containers set them to the data volume (the image's code tree is read-only).
DEFAULT_BACKUPS = Path(__import__("os").environ.get("NOVA_BACKUP_DIR") or BACKEND_DIR / "data" / "backups")
DEFAULT_STATE = Path(__import__("os").environ.get("NOVA_RELEASE_STATE_DIR") or BACKEND_DIR / "data" / "release-state")


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.release")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check-config", "status", "classify", "integrity", "history"):
        sub.add_parser(name)
    for name in ("backup", "list-backups", "prune", "deploy", "migrate"):
        s = sub.add_parser(name)
        s.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUPS)
        if name == "backup":
            s.add_argument("--label", default="")
        if name == "prune":
            s.add_argument("--keep", type=int, required=True)
        if name in ("migrate", "deploy"):
            s.add_argument("--break-stale-lock", action="store_true")
            s.add_argument("--no-backup", action="store_true", help="skip the pre-migration backup (not recommended)")
    v = sub.add_parser("verify"); v.add_argument("backup", type=Path)
    r = sub.add_parser("restore"); r.add_argument("backup", type=Path); r.add_argument("--target", type=Path, required=True); r.add_argument("--no-migrate", action="store_true")
    m = sub.add_parser("manifest"); m.add_argument("--junit", type=Path); m.add_argument("--out", type=Path); m.add_argument("--sbom", type=Path)
    sm = sub.add_parser("smoke"); sm.add_argument("--url", required=True); sm.add_argument("--wait", type=float, default=0.0); sm.add_argument("--token-env", default="NOVA_API_TOKEN")
    rp = sub.add_parser("rollback-plan"); rp.add_argument("previous_manifest", type=Path)
    return p


def main(argv: list[str] | None = None) -> int:
    import os
    a = build_parser().parse_args(argv)
    s = get_settings()
    root = bk.default_artifact_root(s.artifact_dir)
    try:
        if a.cmd == "check-config":
            f = prodcheck.check(s)
            _out({"environment": s.environment, "findings": [x.as_dict() for x in f]})
            return 1 if any(x.severity == "error" for x in f) else 0
        if a.cmd == "status":
            _out(dbtools.schema_status(s.database_url)); return 0
        if a.cmd == "classify":
            _out(dbtools.classify_migrations()); return 0
        if a.cmd == "integrity":
            r = dbtools.integrity_check(s.database_url)
            r["file_references"] = bk.file_reference_check(s.database_url, root)
            _out(r); return 0 if r.get("ok") and r["file_references"]["ok"] else 1
        if a.cmd == "history":
            _out(deploy.history(DEFAULT_STATE)); return 0
        if a.cmd == "migrate":
            _out(dbtools.migrate(s.database_url, backup_dir=None if a.no_backup else a.backup_dir, break_stale_lock=a.break_stale_lock)); return 0
        if a.cmd == "backup":
            r = bk.create_backup(s.database_url, a.backup_dir, artifact_root=root, label=a.label)
            _out({"path": r["path"], "schema_revision": r["manifest"]["schema_revision"], "artifact_files": len(r["manifest"]["artifacts"])}); return 0
        if a.cmd == "list-backups":
            _out(bk.list_backups(a.backup_dir)); return 0
        if a.cmd == "verify":
            m = bk.verify_dir(a.backup); _out({"valid": True, "schema_revision": m["schema_revision"], "created_at": m["created_at"]}); return 0
        if a.cmd == "restore":
            r = bk.restore(a.backup, a.target, run_migrations=not a.no_migrate); _out(r); return 0 if r["ok"] else 1
        if a.cmd == "prune":
            _out(bk.prune(a.backup_dir, keep=a.keep)); return 0
        if a.cmd == "manifest":
            man = manifest.build(a.junit)
            if a.out:
                a.out.write_text(json.dumps(man, indent=2), encoding="utf-8")
            if a.sbom:
                a.sbom.write_text(json.dumps(manifest.sbom(), indent=2), encoding="utf-8")
            _out({k: man[k] for k in ("version", "source", "migrations")} | {"tests": man["tests"]}); return 0
        if a.cmd == "smoke":
            if a.wait:
                w = smoke.wait_ready(a.url, timeout=a.wait)
                if not w["ready"]:
                    _out({"ok": False, "reason": "not ready", **w}); return 1
            r = smoke.run(a.url, os.environ.get(a.token_env, "")); _out(r); return 0 if r["ok"] else 1
        if a.cmd == "rollback-plan":
            _out(deploy.rollback_plan(s.database_url, json.loads(a.previous_manifest.read_text(encoding="utf-8")))); return 0
        if a.cmd == "deploy":
            pf = deploy.preflight(s, a.backup_dir)
            if not pf["ok"]:
                deploy.record(DEFAULT_STATE, "deploy", "preflight_failed", problems=[x["code"] for x in pf["problems"]]); _out(pf); return 2
            res = dbtools.migrate(s.database_url, backup_dir=None if a.no_backup else a.backup_dir, break_stale_lock=a.break_stale_lock)
            deploy.record(DEFAULT_STATE, "deploy", "migrated" if res["changed"] else "no_migration_needed", backup=res["backup"], schema=res["after"])
            _out({"preflight": pf["warnings"], "migration": res, "next": "start the service, then run: python -m app.release smoke --url <base> --wait 60"}); return 0
    except (dbtools.MigrationError, bk.BackupError, prodcheck.ConfigurationError, OSError, ValueError) as exc:
        deploy.record(DEFAULT_STATE, a.cmd, "failed", error=str(exc)[:300]) if a.cmd in ("deploy", "migrate", "restore", "backup") else None
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
