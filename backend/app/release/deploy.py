"""Deployment record keeping, preflight, and rollback planning. Rollback planning never executes a schema downgrade."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.release import backup, dbtools, manifest, prodcheck

MIN_FREE_BYTES = 200 * 1024 * 1024


def preflight(settings: Settings, backup_dir: Path) -> dict[str, Any]:
    findings = prodcheck.check(settings)
    problems = [f.as_dict() for f in findings if f.severity == "error"]
    backup_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(backup_dir).free
    if free < MIN_FREE_BYTES:
        problems.append({"severity": "error", "code": "disk_low", "message": f"Less than {MIN_FREE_BYTES // (1 << 20)} MiB free at the backup location."})
    status = dbtools.schema_status(settings.database_url)
    if status["state"] == "ahead_or_unknown":
        problems.append({"severity": "error", "code": "schema_unknown", "message": f"Database revision {status['current']} is unknown to this release."})
    return {"ok": not problems, "problems": problems, "warnings": [f.as_dict() for f in findings if f.severity == "warning"], "schema": status, "free_bytes": free}


def record(state_dir: Path, event: str, outcome: str, **detail: Any) -> dict[str, Any]:
    """Append to the deployment history. The history is informational: failing to write it (e.g. a read-only location) is reported on stderr
    and never turns a completed migration into a reported failure."""
    entry = {"at": datetime.now(UTC).isoformat(), "event": event, "outcome": outcome, "version": manifest.APP_VERSION, "revision": manifest.source_revision()["revision"], **detail}
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "deployments.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    except OSError as exc:
        print(f"WARNING: deployment history not written to {state_dir} ({type(exc).__name__}); set NOVA_RELEASE_STATE_DIR to a writable directory.",
              file=sys.stderr)
    return entry


def history(state_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    p = state_dir / "deployments.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()[-limit:]] if p.is_file() else []


def rollback_plan(db_url: str, previous_manifest: dict[str, Any]) -> dict[str, Any]:
    """Can the PREVIOUS release run against the CURRENT schema? Compatible only if the database is at the previous head, or every revision applied after
    the previous head is additive (previous code ignores new tables/columns). Otherwise the data must be restored from a pre-migration backup."""
    prev_head = previous_manifest["migrations"]["head"]
    cur = dbtools.schema_status(db_url)["current"]
    order = dbtools.revisions_in_order()
    cls = {c["revision"]: c for c in dbtools.classify_migrations()}
    plan: dict[str, Any] = {"previous_release": previous_manifest.get("version"), "previous_head": prev_head, "database_revision": cur, "downgrade_executed": False}
    if cur == prev_head:
        return {**plan, "app_rollback": "safe", "schema": "unchanged", "action": "Redeploy the previous release artifact; no data action needed."}
    if cur not in order or prev_head not in order:
        return {**plan, "app_rollback": "unknown", "schema": "unknown", "action": "A revision is unknown to this code; do not guess. Use the pre-migration backup (restore into a new target) after review."}
    applied_after = order[order.index(prev_head) + 1: order.index(cur) + 1] if order.index(cur) > order.index(prev_head) else []
    risky = [r for r in applied_after if not cls[r]["additive"]]
    if applied_after and not risky:
        return {**plan, "app_rollback": "likely_safe", "schema": "forward-compatible (additive only)", "revisions_after_previous": applied_after, "action": "Redeploy the previous release; keep the migrated database. Run smoke tests; extra tables/columns are ignored."}
    return {**plan, "app_rollback": "requires_data_decision", "schema": "not provably compatible", "revisions_after_previous": applied_after, "needs_review": risky,
            "action": "Do NOT downgrade automatically. Either keep the new release, or restore the pre-migration backup into a NEW target (`restore`), validate it, then switch the service to it. Data written since the migration is lost in a restore."}


def latest_pre_migration_backup(backup_dir: Path) -> str | None:
    cands = [b for b in backup.list_backups(backup_dir) if b["valid"] and b["name"].endswith("pre-migration")]
    return cands[-1]["name"] if cands else None
