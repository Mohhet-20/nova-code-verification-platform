"""Test discovery, generation, review, approved execution and coverage endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.db.session import get_db, get_session_factory
from app.models import CoverageFile, TestCandidate, TestExecutionRun, TestFile, TestGenerationRun
from app.observability.jobs import tracked
from app.schemas.common import Page, make_page
from app.schemas.testing import (
    ApproveRequest,
    CandidateOut,
    CoverageFileOut,
    CoverageOut,
    DiscoveryOut,
    ExecuteExistingRequest,
    ExecuteRequest,
    ExecutionRunOut,
    ExportRequest,
    GapOut,
    GenerateRequest,
    GenerationOut,
    PlanOut,
    PlanRequest,
    TestFileOut,
    TestsOverview,
)
from app.security.auth import RequestContext, get_context
from app.services import repositories as svc
from app.services.testing import artifacts, execution, service

router = APIRouter(prefix="/repositories/{repository_id}", tags=["testing"])

NOTE_DISCOVERY = ("Discovered tests were found by static analysis only. Nothing was imported or executed, "
                  "so 'discovered' does not mean 'passing'.")


@tracked("test_execution", "tests", model=TestExecutionRun, failed=("error", "timed_out"))
def _job(run_id: str) -> None:
    with get_session_factory()() as session:
        execution.run_execution(session, get_settings(), run_id)


def _cand(db_c: TestCandidate, settings: Settings, with_source: bool) -> CandidateOut:
    out = CandidateOut.model_validate(db_c)
    if with_source and db_c.staged_path:
        try:
            p = artifacts.resolve_rel(settings, db_c.staged_path)
            if p.is_file():
                out.source = p.read_text(encoding="utf-8", errors="replace")[: settings.test_max_candidate_bytes]
        except Exception:  # noqa: BLE001 - a missing artifact must not break the listing
            out.source = None
    return out


def _gen_out(db: Session, run: TestGenerationRun, settings: Settings, created: bool = True) -> GenerationOut:
    out = GenerationOut.model_validate(run)
    out.candidates = [_cand(c, settings, True) for c in service.candidates_of(db, run.id)]
    out.created = created
    return out


# ------------------------------------------------------------------------------------------------ discovery

@router.post("/tests/discover", response_model=DiscoveryOut, status_code=201)
def discover(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> DiscoveryOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    return DiscoveryOut.model_validate(service.run_discovery(db, ctx, settings, repo))


@router.get("/tests", response_model=TestsOverview)
def tests_overview(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> TestsOverview:
    repo = svc.get_repository(db, ctx, repository_id)
    d = service.latest_discovery(db, repo.id)
    gens = db.scalar(select(func.count()).select_from(TestGenerationRun).where(TestGenerationRun.repository_id == repo.id)) or 0
    m = execution.latest_measured(db, repo.id)
    return TestsOverview(
        discovery=DiscoveryOut.model_validate(d) if d else None,
        note=NOTE_DISCOVERY if d else "Tests have not been discovered yet.", generation_runs=gens,
        candidates_by_status=service.count_by(db, TestCandidate, repo.id, TestCandidate.status),
        execution_runs_by_status=service.count_by(db, TestExecutionRun, repo.id, TestExecutionRun.status),
        latest_coverage_run_id=m.id if m else None)


@router.get("/tests/files", response_model=Page[TestFileOut])
def test_files(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
               q: str | None = Query(None, max_length=200), ctx: RequestContext = Depends(get_context),
               db: Session = Depends(get_db)) -> Page[TestFileOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    d = service.latest_discovery(db, repo.id)
    if d is None:
        return make_page([], 0, page, page_size)
    cond = [TestFile.discovery_id == d.id]
    if q:
        cond.append(TestFile.rel_path.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    total = db.scalar(select(func.count()).select_from(TestFile).where(*cond)) or 0
    rows = db.scalars(select(TestFile).where(*cond).order_by(TestFile.rel_path).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([TestFileOut.model_validate(r) for r in rows], total, page, page_size)


# ------------------------------------------------------------------------------------------------ planning / generation

@router.post("/tests/plan", response_model=PlanOut)
def plan(repository_id: str, body: PlanRequest, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
         settings: Settings = Depends(get_settings)) -> PlanOut:
    repo = svc.get_repository(db, ctx, repository_id)
    items = service.plan_targets(db, repo, settings, [t.model_dump() for t in body.targets], body.include_templates)
    return PlanOut(targets=items, note="A plan is a preview. Nothing was written and nothing was run.")


@router.post("/tests/generate", response_model=GenerationOut, status_code=201)
def generate(repository_id: str, body: GenerateRequest, ctx: RequestContext = Depends(get_context),
             db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> GenerationOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run, created = service.generate(db, ctx, settings, repo, [t.model_dump() for t in body.targets], body.include_templates)
    return _gen_out(db, run, settings, created)


@router.get("/tests/generations", response_model=Page[GenerationOut])
def list_generations(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[GenerationOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    total = db.scalar(select(func.count()).select_from(TestGenerationRun).where(TestGenerationRun.repository_id == repo.id)) or 0
    rows = db.scalars(select(TestGenerationRun).where(TestGenerationRun.repository_id == repo.id)
                      .order_by(TestGenerationRun.created_at.desc(), TestGenerationRun.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([GenerationOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/tests/generations/{run_id}", response_model=GenerationOut)
def get_generation(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> GenerationOut:
    repo = svc.get_repository(db, ctx, repository_id)
    return _gen_out(db, service.get_run(db, repo.id, run_id), settings)


@router.post("/tests/generations/{run_id}/validate", response_model=GenerationOut)
def validate(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> GenerationOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run = service.get_run(db, repo.id, run_id)
    service.validate_run(db, ctx, settings, repo, run)
    return _gen_out(db, run, settings)


@router.post("/tests/generations/{run_id}/approve", response_model=GenerationOut)
def approve(repository_id: str, run_id: str, body: ApproveRequest | None = None,
            ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings)) -> GenerationOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run = service.get_run(db, repo.id, run_id)
    service.approve(db, ctx, settings, repo, run, body.candidate_ids if body else None)
    return _gen_out(db, run, settings)


@router.post("/tests/generations/{run_id}/execute", response_model=ExecutionRunOut, status_code=202)
def execute_generated(repository_id: str, run_id: str, background: BackgroundTasks, body: ExecuteRequest | None = None,
                      ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> ExecutionRunOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    b = body or ExecuteRequest()
    run = execution.begin_generated(db, ctx, settings, repo, run_id, b.candidate_ids, b.coverage)
    background.add_task(_job, run.id)
    return ExecutionRunOut.model_validate(run)


@router.post("/tests/generations/{run_id}/candidates/{candidate_id}/export", response_model=CandidateOut)
def export_candidate(repository_id: str, run_id: str, candidate_id: str, body: ExportRequest,
                     ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> CandidateOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run = service.get_run(db, repo.id, run_id)
    cand = next((c for c in service.candidates_of(db, run.id) if c.id == candidate_id), None)
    if cand is None:
        raise NotFoundError("Candidate not found.")
    return _cand(service.export_candidate(db, ctx, settings, repo, cand, body.destination, body.overwrite), settings, False)


# ------------------------------------------------------------------------------------------------ runs

@router.post("/tests/execute", response_model=ExecutionRunOut, status_code=202)
def execute_existing(repository_id: str, body: ExecuteExistingRequest, background: BackgroundTasks,
                     ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> ExecutionRunOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run = execution.begin_existing(db, ctx, settings, repo, body.coverage)
    background.add_task(_job, run.id)
    return ExecutionRunOut.model_validate(run)


@router.get("/tests/runs", response_model=Page[ExecutionRunOut])
def list_runs(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[ExecutionRunOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    total = db.scalar(select(func.count()).select_from(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id)) or 0
    rows = db.scalars(select(TestExecutionRun).where(TestExecutionRun.repository_id == repo.id)
                      .order_by(TestExecutionRun.created_at.desc(), TestExecutionRun.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([ExecutionRunOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/tests/runs/{run_id}", response_model=ExecutionRunOut)
def get_run(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> ExecutionRunOut:
    repo = svc.get_repository(db, ctx, repository_id)
    return ExecutionRunOut.model_validate(execution.get_exec_run(db, repo.id, run_id))


# ------------------------------------------------------------------------------------------------ coverage

@router.get("/coverage", response_model=CoverageOut)
def coverage(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> CoverageOut:
    repo = svc.get_repository(db, ctx, repository_id)
    run = execution.latest_measured(db, repo.id)
    if run is None:
        return CoverageOut(state="never_measured", run=None,
                           message="No coverage has been measured. Percentages appear only after an approved run measures them.")
    basis = ("the repository's own test suite" if run.kind == "existing"
             else "generated tests only; this is not the coverage of the whole suite")
    return CoverageOut(state="measured", run=ExecutionRunOut.model_validate(run), basis=basis,
                       unmeasured_targets=list(run.environment_profile.get("unmeasured_targets", [])),
                       message="Measured by coverage.py with branch tracking for the listed scope.")


@router.get("/coverage/files", response_model=Page[CoverageFileOut])
def coverage_files(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                   sort: str = Query("percent", pattern="^(percent|path|missing)$"), q: str | None = Query(None, max_length=200),
                   run_id: str | None = Query(None, max_length=32),
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[CoverageFileOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    run = execution.get_exec_run(db, repo.id, run_id) if run_id else execution.latest_measured(db, repo.id)
    if run is None:
        return make_page([], 0, page, page_size)
    cond: list[Any] = [CoverageFile.execution_run_id == run.id]
    if q:
        cond.append(CoverageFile.rel_path.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    order = {"percent": [CoverageFile.line_percent.asc(), CoverageFile.rel_path], "path": [CoverageFile.rel_path],
             "missing": [(CoverageFile.statements - CoverageFile.covered).desc(), CoverageFile.rel_path]}[sort]
    total = db.scalar(select(func.count()).select_from(CoverageFile).where(*cond)) or 0
    rows = db.scalars(select(CoverageFile).where(*cond).order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([CoverageFileOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/coverage/gaps", response_model=GapOut)
def coverage_gaps(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                  path: str | None = Query(None, max_length=300), include_private: bool = Query(False),
                  ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> GapOut:
    repo = svc.get_repository(db, ctx, repository_id)
    g = execution.compute_gaps(db, settings, repo, path=path, include_private=include_private)
    items = g["items"]
    return GapOut(items=items[(page - 1) * page_size: page * page_size], total=len(items), page=page, page_size=page_size,
                  coverage_run_id=g["coverage_run_id"], discovery_id=g["discovery_id"],
                  note="Items marked 'static' are recommendations from code structure, not measurements. "
                       "Only items with measured missing lines are backed by coverage data.")
