"""Persistence-backed workflow: discovery, planning, generation, validation, approval, export."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, UnsafePathError, ValidationFailedError
from app.db.base import utcnow
from app.models import (
    Finding,
    IndexedFile,
    Repository,
    TestCandidate,
    TestDiscovery,
    TestFile,
    TestGenerationRun,
)
from app.security.auth import RequestContext
from app.security.paths import resolve_repository_root, safe_join
from app.services import audit
from app.services import repositories as repo_svc
from app.services.testing import artifacts, discovery, generator, planner, validator
from app.services.testing.modules import module_info

KEEP_DISCOVERIES = 5
NOVA_MARKER = "# NOVA-GENERATED TEST CANDIDATE"


def published_snapshot(db: Session, repo: Repository):  # type: ignore[no-untyped-def]
    snap = repo_svc.latest_snapshot(db, repo.id, completed_only=True)
    if snap is None:
        raise ConflictError("This repository has no published index yet. Index it first.")
    return snap


def python_files(db: Session, snapshot_id: str) -> dict[str, str]:
    rows = db.execute(select(IndexedFile.rel_path, IndexedFile.sha256).where(
        IndexedFile.snapshot_id == snapshot_id, IndexedFile.language == "Python")).all()
    return {r.rel_path: r.sha256 for r in rows}


# ------------------------------------------------------------------------------------------------ discovery

def run_discovery(db: Session, ctx: RequestContext, settings: Settings, repo: Repository) -> TestDiscovery:
    snap = published_snapshot(db, repo)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    files = python_files(db, snap.id)
    cfg, found, errors, excluded, stats = discovery.discover(
        root, files, max_files=settings.test_max_files_discovered, max_bytes=settings.test_max_file_bytes)
    unparsable = sum(1 for f in found if f.parse_status != "ok")
    d = TestDiscovery(
        repository_id=repo.id, index_snapshot_id=snap.id, framework="pytest", framework_config=cfg.as_dict(),
        status="completed_with_warnings" if (unparsable or errors) else "completed", finished_at=utcnow(),
        files_found=len(found), files_unparsable=unparsable, tests_found=sum(len(f.tests) for f in found),
        source_files_with_tests=stats["source_files_with_tests"], source_files_total=stats["source_files_total"],
        errors=[*errors, *[f"{f.rel_path}: {f.parse_error}" for f in found if f.parse_status == "syntax_error"]][:50],
        excluded=excluded[:50], requested_by=ctx.user.id)
    db.add(d)
    db.flush()
    for f in found:
        db.add(TestFile(discovery_id=d.id, repository_id=repo.id, rel_path=f.rel_path, sha256=f.sha256,
                        parse_status=f.parse_status, parse_error=f.parse_error, test_count=len(f.tests),
                        tests=f.tests[:500], associated_sources=f.associated[:50], notes=f.notes))
    old = db.scalars(select(TestDiscovery.id).where(TestDiscovery.repository_id == repo.id)
                     .order_by(TestDiscovery.started_at.desc(), TestDiscovery.id.desc()).offset(KEEP_DISCOVERIES)).all()
    if old:
        db.execute(delete(TestDiscovery).where(TestDiscovery.id.in_(old)))
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.discovered",
                 entity_type="repository", entity_id=repo.id, details={"files": len(found), "tests": d.tests_found})
    db.commit()
    return d


def latest_discovery(db: Session, repo_id: str) -> TestDiscovery | None:
    return db.scalar(select(TestDiscovery).where(TestDiscovery.repository_id == repo_id)
                     .order_by(TestDiscovery.started_at.desc(), TestDiscovery.id.desc()).limit(1))


def is_test_path(db: Session, repo_id: str, rel: str) -> bool:
    d = latest_discovery(db, repo_id)
    cfg = discovery.PytestConfig()
    if d is not None:
        for k in ("python_files", "python_classes", "python_functions", "testpaths"):
            v = d.framework_config.get(k)
            if v:
                setattr(cfg, k, v)
    return discovery.is_test_file(rel, cfg)


# ------------------------------------------------------------------------------------------------ targets, plan

def _read_target(root: Path, rel: str, settings: Settings) -> tuple[str, str]:
    path = safe_join(root, rel)
    if path.is_symlink() or not path.is_file():
        raise ValidationFailedError(f"Target '{rel}' is not a regular file.")
    if path.stat().st_size > settings.test_max_file_bytes:
        raise ValidationFailedError(f"Target '{rel}' is larger than {settings.test_max_file_bytes} bytes.")
    data = path.read_bytes()
    return data.decode("utf-8", errors="replace"), hashlib.sha256(data).hexdigest()


def resolve_targets(db: Session, repo: Repository, settings: Settings, targets: list[dict[str, Any]]
                    ) -> tuple[Path, Any, list[dict[str, Any]]]:
    snap = published_snapshot(db, repo)
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    files = python_files(db, snap.id)
    out: list[dict[str, Any]] = []
    for t in targets:
        rel = t["path"].replace("\\", "/")
        if rel not in files:
            raise NotFoundError(f"'{rel}' is not an indexed Python file of this repository.")
        if is_test_path(db, repo.id, rel):
            raise ValidationFailedError(f"'{rel}' is a test file; choose source code to generate tests for.")
        text, sha = _read_target(root, rel, settings)
        out.append({"path": rel, "symbol": t.get("symbol"), "sha256": sha, "text": text, "indexed_sha256": files[rel]})
    return root, snap, out


def _findings_for(db: Session, repo_id: str, rel: str) -> list[dict[str, Any]]:
    rows = db.execute(select(Finding.rule_id, Finding.start_line, Finding.title).where(
        Finding.repository_id == repo_id, Finding.rel_path == rel, Finding.status.in_(("new", "existing", "reopened")))).all()
    return [{"rule_id": r.rule_id, "start_line": r.start_line, "title": r.title} for r in rows]


def plan_targets(db: Session, repo: Repository, settings: Settings, targets: list[dict[str, Any]], include_templates: bool
                 ) -> list[dict[str, Any]]:
    _, _, resolved = resolve_targets(db, repo, settings, targets)
    files = set(python_files(db, published_snapshot(db, repo).id))
    out: list[dict[str, Any]] = []
    for t in resolved:
        info = module_info(t["path"], files)
        try:
            b = generator.build(t["path"], t["text"], t["symbol"], info, _findings_for(db, repo.id, t["path"]),
                                include_templates=include_templates)
        except (ValueError, LookupError) as exc:
            raise ValidationFailedError(str(exc)) from None
        warns = list(b.warnings)
        if t["sha256"] != t["indexed_sha256"]:
            warns.append("The file changed since it was indexed; symbols and findings may be stale.")
        out.append({"path": t["path"], "symbol": t["symbol"], "module": b.module, "import_root": b.import_root,
                    "cases": [c.public() for c in b.cases], "warnings": warns})
    return out


# ------------------------------------------------------------------------------------------------ generation

def tool_versions() -> dict[str, Any]:
    return {"generator": planner.GENERATOR_VERSION, "validator": "1.0"}


def generate(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, targets: list[dict[str, Any]],
             include_templates: bool) -> tuple[TestGenerationRun, bool]:
    """(run, created). An identical request (same targets, same file hashes) returns the existing run."""
    if len(targets) > 10:
        raise ValidationFailedError("At most 10 targets per generation request.")
    root, snap, resolved = resolve_targets(db, repo, settings, targets)
    req = json.dumps({"r": repo.id, "t": sorted((t["path"], t["symbol"] or "", t["sha256"]) for t in resolved),
                      "g": planner.GENERATOR_VERSION, "tpl": include_templates}, sort_keys=True)
    rhash = hashlib.sha256(req.encode()).hexdigest()
    existing = db.scalar(select(TestGenerationRun).where(
        TestGenerationRun.repository_id == repo.id, TestGenerationRun.request_hash == rhash,
        TestGenerationRun.status != "rejected").order_by(TestGenerationRun.created_at.desc()).limit(1))
    if existing is not None:
        return existing, False
    files = set(python_files(db, snap.id))
    run = TestGenerationRun(
        repository_id=repo.id, index_snapshot_id=snap.id, status="generated", request_hash=rhash,
        targets=[{"path": t["path"], "symbol": t["symbol"], "sha256": t["sha256"]} for t in resolved],
        generator_version=planner.GENERATOR_VERSION, tool_versions=tool_versions(), created_by=ctx.user.id)
    db.add(run)
    db.flush()
    run_path, rel_dir = artifacts.run_dir(settings, repo.id, "generated", run.id)
    run.artifact_dir = rel_dir
    warnings: list[str] = []
    used: set[str] = set()
    made = 0
    for t in resolved:
        info = module_info(t["path"], files)
        try:
            b = generator.build(t["path"], t["text"], t["symbol"], info, _findings_for(db, repo.id, t["path"]),
                                include_templates=include_templates)
        except (ValueError, LookupError) as exc:
            warnings.append(f"{t['path']}: {exc}")
            continue
        if made >= settings.test_max_candidates_per_run:
            warnings.append("Candidate limit reached; remaining targets were not generated.")
            break
        data = b.source.encode("utf-8")
        stem, n = b.file_stem, 1
        while stem in used:
            n += 1
            stem = f"{b.file_stem}_{n}"
        used.add(stem)
        name = f"{stem}.py"
        artifacts.write_new(run_path / name, data)
        runnable = sum(1 for c in b.cases if c.runnable)
        conf = "low" if any(c.confidence == "low" for c in b.cases) or not b.cases else \
            "medium" if any(c.confidence == "medium" for c in b.cases) else "high"
        db.add(TestCandidate(
            run_id=run.id, repository_id=repo.id, target_path=t["path"], target_symbol=t["symbol"], module=b.module,
            import_root=b.import_root, cases=[c.public() for c in b.cases], confidence=conf,
            review_required=any(c.review_required for c in b.cases), runnable_count=runnable,
            staged_path=f"{rel_dir}/{name}", content_sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
            status="generated", warnings=b.warnings))
        made += 1
    if made == 0:
        db.rollback()
        raise ValidationFailedError("No candidate could be generated: " + "; ".join(warnings or ["no targets"]))
    run.warnings = warnings
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.generated",
                 entity_type="test_generation_run", entity_id=run.id, details={"repository_id": repo.id, "candidates": made})
    db.commit()
    return run, True


def candidates_of(db: Session, run_id: str) -> list[TestCandidate]:
    return list(db.scalars(select(TestCandidate).where(TestCandidate.run_id == run_id).order_by(TestCandidate.staged_path, TestCandidate.id)))


def get_run(db: Session, repo_id: str, run_id: str) -> TestGenerationRun:
    run = db.scalar(select(TestGenerationRun).where(TestGenerationRun.id == run_id, TestGenerationRun.repository_id == repo_id))
    if run is None:
        raise NotFoundError("Generation run not found.")
    return run


def refresh_run_status(db: Session, run: TestGenerationRun) -> None:
    statuses = [c.status for c in candidates_of(db, run.id)]
    if not statuses:
        return
    run.status = ("approved" if "approved" in statuses else "validated" if "validated" in statuses
                  else "rejected" if all(s == "rejected" for s in statuses) else "generated")


def validate_run(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, run: TestGenerationRun) -> list[TestCandidate]:
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    run_path, _ = artifacts.run_dir(settings, repo.id, "generated", run.id)
    target_sha = {t["path"]: t["sha256"] for t in run.targets}
    seen: set[str] = set()
    cands = candidates_of(db, run.id)
    for c in cands:
        try:
            staged = artifacts.resolve_rel(settings, c.staged_path)
            ok, detail = validator.path_inside(staged, run_path)
            data = staged.read_bytes() if ok else b""
        except (UnsafePathError, OSError):
            ok, detail, data = False, "staged file is missing or outside the artifact root", b""
        try:
            now = hashlib.sha256(safe_join(root, c.target_path).read_bytes()).hexdigest()
        except (UnsafePathError, OSError):
            now = None
        checks = validator.validate(
            data, max_bytes=settings.test_max_candidate_bytes, expected_module=c.module, recorded_sha=c.content_sha256,
            target_sha_recorded=target_sha.get(c.target_path, ""), target_sha_now=now, other_hashes=set(seen),
            path_ok=ok, path_detail=detail)
        seen.add(c.content_sha256)
        problems = validator.failed(checks)
        c.validation = [x.as_dict() for x in checks]
        c.rejection_reasons = problems
        if problems:
            c.status, c.approved_by, c.approved_at, c.approved_sha256 = "rejected", None, None, None
        elif c.status in ("generated", "rejected"):
            c.status = "validated"
    run.validated_at = utcnow()
    refresh_run_status(db, run)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.validated",
                 entity_type="test_generation_run", entity_id=run.id,
                 details={"validated": sum(c.status == "validated" for c in cands), "rejected": sum(c.status == "rejected" for c in cands)})
    db.commit()
    return cands


def approve(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, run: TestGenerationRun,
            candidate_ids: list[str] | None) -> list[TestCandidate]:
    cands = candidates_of(db, run.id)
    chosen = [c for c in cands if candidate_ids is None or c.id in candidate_ids]
    if candidate_ids is not None:
        missing = set(candidate_ids) - {c.id for c in cands}
        if missing:
            raise NotFoundError("One or more candidates do not belong to this generation run.")
    if not chosen:
        raise ValidationFailedError("No candidates selected.")
    bad = [c for c in chosen if c.status not in ("validated", "approved")]
    if bad:
        raise ConflictError("Only validated candidates can be approved. Validate the run first; rejected candidates cannot be approved.",
                            {"candidate_ids": [c.id for c in bad], "statuses": sorted({c.status for c in bad})})
    for c in chosen:
        staged = artifacts.resolve_rel(settings, c.staged_path)
        if artifacts.sha256_file(staged) != c.content_sha256:
            raise ConflictError("A staged file no longer matches what was generated; validate again.", {"candidate_id": c.id})
    now = utcnow()
    for c in chosen:
        if c.status != "approved":
            c.status, c.approved_by, c.approved_at, c.approved_sha256 = "approved", ctx.user.id, now, c.content_sha256
    refresh_run_status(db, run)
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.approved",
                 entity_type="test_generation_run", entity_id=run.id, details={"candidates": [c.id for c in chosen]})
    db.commit()
    return cands


def export_candidate(db: Session, ctx: RequestContext, settings: Settings, repo: Repository, cand: TestCandidate,
                     destination: str, overwrite: bool) -> TestCandidate:
    """Copy an approved candidate into the repository. Never replaces a file that is not NOVA-generated."""
    if cand.status != "approved":
        raise ConflictError("Only an approved candidate can be exported.")
    dest = destination.replace("\\", "/").strip()
    if not dest.endswith(".py") or dest.rsplit("/", 1)[-1].startswith(".") or dest.count("/") > 6 or len(dest) > 300:
        raise ValidationFailedError("Destination must be a repository-relative .py path.")
    if not discovery.is_test_file(dest, discovery.PytestConfig()):
        raise ValidationFailedError("Destination file name must match test_*.py or *_test.py so pytest collects it.")
    root = resolve_repository_root(repo.root_path, settings.registration_base_dirs)
    target = safe_join(root, dest)
    staged = artifacts.resolve_rel(settings, cand.staged_path)
    data = staged.read_bytes()
    if hashlib.sha256(data).hexdigest() != cand.approved_sha256:
        raise ConflictError("The staged file changed after approval; review and approve it again.")
    if target.exists() or target.is_symlink():
        existing_ok = target.is_file() and not target.is_symlink() and target.read_bytes()[:200].decode("utf-8", "replace").startswith(NOVA_MARKER)
        if not (overwrite and existing_ok):
            raise ConflictError("A file already exists at the destination. NOVA never overwrites user-authored files"
                                + (" (overwrite is only allowed for files NOVA itself generated)." if overwrite else "."))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    cand.exported_path = dest
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action="tests.exported",
                 entity_type="test_candidate", entity_id=cand.id, details={"repository_id": repo.id, "path": dest})
    db.commit()
    return cand


def count_by(db: Session, model: Any, repo_id: str, col: Any) -> dict[str, int]:
    return {str(k): int(n) for k, n in db.execute(select(col, func.count()).where(model.repository_id == repo_id).group_by(col))}
