"""Metadata-only staleness check shared by /freshness, /index/status and /metrics."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import IndexedFile, Repository
from app.schemas.intelligence import Freshness
from app.security.paths import resolve_repository_root
from app.services import repositories as svc
from app.services.indexer import scan_repository


def compute_freshness(db: Session, repo: Repository, settings: Settings) -> Freshness:
    """Compares file size and mtime with the published index. Nothing is read or hashed."""
    latest = svc.latest_snapshot(db, repo.id)
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    if latest is not None and latest.status == "running":
        return Freshness(state="running", snapshot_id=snap.id if snap else None,
                         generation=snap.generation if snap else None, last_attempt_status="running",
                         note="An indexing run is in progress.")
    if snap is None:
        return Freshness(state="never_indexed", last_attempt_status=latest.status if latest else None,
                         note="No published index exists for this repository.")
    indexed = {p: (sz, mt) for p, sz, mt in db.execute(
        select(IndexedFile.rel_path, IndexedFile.size_bytes, IndexedFile.mtime_ns)
        .where(IndexedFile.snapshot_id == snap.id))}
    current: dict[str, tuple[int, int]] = {}
    try:
        root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
        result = scan_repository(root, svc.limits_from(settings), lambda f: None, read_contents=False,
                                 on_stat=lambda rel, size, mt: current.__setitem__(rel, (size, mt)))
    except Exception:
        return Freshness(state="unknown", snapshot_id=snap.id, generation=snap.generation,
                         indexed_at=snap.finished_at, note="The repository directory could not be inspected.")
    changed = sorted(p for p, v in current.items() if p in indexed and indexed[p] != v)
    new = sorted(p for p in current if p not in indexed)
    removed = [] if result.truncated else sorted(p for p in indexed if p not in current)
    head_changed = None
    if repo.is_git and snap.git_head and repo.git_head:
        head_changed = snap.git_head != repo.git_head
    stale = bool(changed or new or removed)
    note = ("Metadata-only estimate (size and modification time); new files may include ones the indexer "
            "would skip.")
    if result.truncated:
        note += " The inspection was truncated, so removals are not reported."
    return Freshness(
        state="stale" if stale else ("unknown" if result.truncated else "fresh"), snapshot_id=snap.id,
        generation=snap.generation, indexed_at=snap.finished_at,
        last_attempt_status=latest.status if latest else None, changed=len(changed), new=len(new),
        removed=None if result.truncated else len(removed), sample_paths=(changed + new + removed)[:20],
        git_head_changed=head_changed, note=note)
