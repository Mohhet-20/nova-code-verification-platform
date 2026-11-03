"""Static analysis endpoints: runs, findings, summary, rule catalog and effective configuration."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, NovaError, ValidationFailedError
from app.db.base import utcnow
from app.db.session import get_db, get_session_factory
from app.models import AnalysisRun, Finding, IndexedFile
from app.observability.jobs import tracked
from app.schemas.analysis import (
    AnalysisRequest,
    AnalysisStatus,
    AnalyzerOut,
    ConfigOut,
    ConfigState,
    CountItem,
    FindingOut,
    FindingPatch,
    LanguageSupport,
    OptionOut,
    RuleOut,
    RulesOut,
    RunOut,
    Summary,
)
from app.schemas.common import Page, make_page
from app.security.auth import RequestContext, get_context
from app.security.paths import resolve_repository_root
from app.services import audit
from app.services import repositories as svc
from app.services.analysis import catalog, orchestrator
from app.services.analysis import config as cfg_mod
from app.services.analysis.model import CATEGORIES, OPEN_STATUSES, SEVERITIES, STATUSES
from app.services.analysis.redaction import scrub

router = APIRouter(prefix="/repositories/{repository_id}", tags=["analysis"])

SORTS = ("severity", "last_seen", "first_seen", "path", "rule", "line")
LITERAL_NOTE = ("Configuration is read from .nova/analysis.json inside the repository and is read-only here. "
                "It can tune or disable rules and exclude files; it cannot change server limits or access control.")


def _esc(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@tracked("static_analysis", "static_analysis", model=AnalysisRun, failed=("failed",))
def _job(run_id: str) -> None:
    with get_session_factory()() as session:
        orchestrator.run_analysis(session, get_settings(), run_id)


def _config_state(repo_root_path: str, settings: Settings) -> tuple[cfg_mod.EffectiveConfig, ConfigState]:
    try:
        root = resolve_repository_root(repo_root_path, settings.registration_base_dirs)
        eff = cfg_mod.load_for_repository(root)
        return eff, ConfigState(valid=True, source=eff.source)
    except cfg_mod.InvalidAnalysisConfig as exc:
        return cfg_mod.defaults(), ConfigState(valid=False, source="repository", errors=[str(d) for d in (exc.details or [])])
    except NovaError as exc:
        return cfg_mod.defaults(), ConfigState(valid=False, source="unavailable", errors=[exc.message])


def _finding_out(f: Finding, file_ids: dict[str, str]) -> FindingOut:
    out = FindingOut.model_validate(f)
    out.file_id = file_ids.get(f.rel_path)
    return out


def _file_ids(db: Session, repo_id: str, paths: list[str]) -> dict[str, str]:
    snap = svc.latest_snapshot(db, repo_id, completed_only=True)
    if snap is None or not paths:
        return {}
    return dict(db.execute(select(IndexedFile.rel_path, IndexedFile.id).where(
        IndexedFile.snapshot_id == snap.id, IndexedFile.rel_path.in_(set(paths)))).all())


# ------------------------------------------------------------------------------------------------ runs

@router.post("/analysis", response_model=RunOut, status_code=202)
def start_analysis(repository_id: str, background: BackgroundTasks, body: AnalysisRequest | None = None,
                   ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> RunOut:
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    run = orchestrator.begin_run(db, ctx, settings, repo, (body or AnalysisRequest()).mode)
    background.add_task(_job, run.id)
    return RunOut.model_validate(run)


@router.get("/analysis/status", response_model=AnalysisStatus)
def analysis_status(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> AnalysisStatus:
    repo = svc.get_repository(db, ctx, repository_id)
    latest = db.scalar(select(AnalysisRun).where(AnalysisRun.repository_id == repo.id)
                       .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc()).limit(1))
    snap = svc.latest_snapshot(db, repo.id, completed_only=True)
    open_count = db.scalar(select(func.count()).select_from(Finding).where(
        Finding.repository_id == repo.id, Finding.status.in_(OPEN_STATUSES))) or 0
    _, cstate = _config_state(repo.root_path, settings)
    analyzed_gen = None
    if latest is not None and latest.index_snapshot_id:
        from app.models import IndexSnapshot
        analyzed = db.get(IndexSnapshot, latest.index_snapshot_id)
        analyzed_gen = analyzed.generation if analyzed else None
    state = latest.status if latest else "never_analyzed"
    messages = {
        "never_analyzed": "This repository has not been analysed.",
        "running": "Analysis is in progress; findings below are from earlier runs.",
        "completed": "The latest run finished without warnings.",
        "completed_with_warnings": "The latest run finished, but some files or rules were not fully analysed. See the warnings.",
        "failed": "The latest run failed. Existing findings were not changed.",
    }
    return AnalysisStatus(
        state=state, latest=RunOut.model_validate(latest) if latest else None, findings_open=int(open_count),
        index_generation=snap.generation if snap else None, analyzed_index_generation=analyzed_gen,
        index_changed_since_run=(snap.generation != analyzed_gen) if snap and analyzed_gen is not None else None,
        config=cstate, message=messages.get(state, state))


@router.get("/analysis/runs", response_model=Page[RunOut])
def list_runs(repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[RunOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    total = db.scalar(select(func.count()).select_from(AnalysisRun).where(AnalysisRun.repository_id == repo.id)) or 0
    rows = db.scalars(select(AnalysisRun).where(AnalysisRun.repository_id == repo.id)
                      .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return make_page([RunOut.model_validate(r) for r in rows], total, page, page_size)


@router.get("/analysis/runs/{run_id}", response_model=RunOut)
def get_run(repository_id: str, run_id: str, ctx: RequestContext = Depends(get_context),
            db: Session = Depends(get_db)) -> RunOut:
    repo = svc.get_repository(db, ctx, repository_id)
    run = db.scalar(select(AnalysisRun).where(AnalysisRun.id == run_id, AnalysisRun.repository_id == repo.id))
    if run is None:
        raise NotFoundError("Analysis run not found.")
    return RunOut.model_validate(run)


# ------------------------------------------------------------------------------------------------ findings

@router.get("/findings", response_model=Page[FindingOut])
def list_findings(
        repository_id: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
        severity: list[Literal["critical", "high", "medium", "low", "info"]] | None = Query(None),
        category: list[Literal["correctness", "maintainability", "reliability", "security", "complexity"]] | None = Query(None),
        status: list[Literal["new", "existing", "resolved", "reopened", "suppressed"]] | None = Query(None),
        rule_id: list[str] | None = Query(None, max_length=20),
        language: str | None = Query(None, max_length=40), path: str | None = Query(None, max_length=300),
        q: str | None = Query(None, max_length=200),
        min_confidence: Literal["high", "medium", "low"] | None = Query(None),
        sort: Literal["severity", "last_seen", "first_seen", "path", "rule", "line"] = Query("severity"),
        order: Literal["asc", "desc"] = Query("asc"),
        ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db)) -> Page[FindingOut]:
    repo = svc.get_repository(db, ctx, repository_id)
    cond = [Finding.repository_id == repo.id]
    if severity:
        cond.append(Finding.severity.in_(severity))
    if category:
        cond.append(Finding.category.in_(category))
    if status:
        cond.append(Finding.status.in_(status))
    if rule_id:
        cond.append(Finding.rule_id.in_([r[:40] for r in rule_id]))
    if language:
        cond.append(Finding.language == language)
    if path and path.strip():
        cond.append(func.lower(Finding.rel_path).like(f"%{_esc(path.strip().lower())}%", escape="\\"))
    if q and q.strip():
        like = f"%{_esc(q.strip().lower())}%"
        cond.append(or_(func.lower(Finding.title).like(like, escape="\\"), func.lower(Finding.rel_path).like(like, escape="\\"),
                        func.lower(Finding.rule_id).like(like, escape="\\")))
    if min_confidence:
        allowed = {"high": ("high",), "medium": ("high", "medium"), "low": ("high", "medium", "low")}[min_confidence]
        cond.append(Finding.confidence.in_(allowed))
    sev_rank = case(*[(Finding.severity == s, i) for i, s in enumerate(SEVERITIES)], else_=len(SEVERITIES))
    key = {"severity": sev_rank, "last_seen": Finding.last_seen_at, "first_seen": Finding.first_seen_at,
           "path": Finding.rel_path, "rule": Finding.rule_id, "line": Finding.start_line}[sort]
    ordering = [key.asc() if order == "asc" else key.desc()]
    if sort != "path":
        ordering.append(Finding.rel_path.asc())
    ordering += [Finding.start_line.asc(), Finding.id.asc()]
    total = db.scalar(select(func.count()).select_from(Finding).where(*cond)) or 0
    rows = db.scalars(select(Finding).where(*cond).order_by(*ordering).offset((page - 1) * page_size).limit(page_size)).all()
    fids = _file_ids(db, repo.id, [r.rel_path for r in rows])
    return make_page([_finding_out(r, fids) for r in rows], total, page, page_size)


def _get_finding(db: Session, repo_id: str, finding_id: str) -> Finding:
    f = db.scalar(select(Finding).where(Finding.id == finding_id, Finding.repository_id == repo_id))
    if f is None:
        raise NotFoundError("Finding not found.")
    return f


@router.get("/findings/{finding_id}", response_model=FindingOut)
def get_finding(repository_id: str, finding_id: str, ctx: RequestContext = Depends(get_context),
                db: Session = Depends(get_db)) -> FindingOut:
    repo = svc.get_repository(db, ctx, repository_id)
    f = _get_finding(db, repo.id, finding_id)
    return _finding_out(f, _file_ids(db, repo.id, [f.rel_path]))


@router.patch("/findings/{finding_id}", response_model=FindingOut)
def update_finding(repository_id: str, finding_id: str, body: FindingPatch, ctx: RequestContext = Depends(get_context),
                   db: Session = Depends(get_db)) -> FindingOut:
    """Suppress an open finding (with a reason) or lift a suppression. Analysis owns every other transition."""
    ctx.require_write()
    repo = svc.get_repository(db, ctx, repository_id)
    f = _get_finding(db, repo.id, finding_id)
    if body.status == "suppressed":
        if f.status == "resolved":
            raise ConflictError("A resolved finding cannot be suppressed; it is no longer reported.")
        if not body.reason or len(body.reason) < 5:
            raise ValidationFailedError("A reason of at least 5 characters is required to suppress a finding.")
        f.status, f.suppression_reason = "suppressed", scrub(body.reason, 300)
    else:  # lift suppression
        if f.status != "suppressed":
            raise ConflictError(f"Only a suppressed finding can be reinstated (current status: {f.status}).")
        f.status, f.suppression_reason = "existing", None
    f.status_changed_by = ctx.user.id
    audit.record(db, workspace_id=ctx.workspace.id, actor_user_id=ctx.user.id, action=f"finding.{body.status}",
                 entity_type="finding", entity_id=f.id, details={"repository_id": repo.id, "rule_id": f.rule_id})
    db.commit()
    db.refresh(f)
    return _finding_out(f, _file_ids(db, repo.id, [f.rel_path]))


# ------------------------------------------------------------------------------------------------ summary / rules / config

@router.get("/analysis/summary", response_model=Summary)
def analysis_summary(repository_id: str, ctx: RequestContext = Depends(get_context),
                     db: Session = Depends(get_db)) -> Summary:
    repo = svc.get_repository(db, ctx, repository_id)
    latest = db.scalar(select(AnalysisRun).where(AnalysisRun.repository_id == repo.id)
                       .order_by(AnalysisRun.started_at.desc(), AnalysisRun.id.desc()).limit(1))
    base = Finding.repository_id == repo.id

    def group(col, *where):  # type: ignore[no-untyped-def]
        return {str(k): int(n) for k, n in db.execute(select(col, func.count()).where(base, *where).group_by(col))}

    by_status = group(Finding.status)
    open_where = Finding.status.in_(OPEN_STATUSES)
    top = db.execute(select(Finding.rule_id, func.count().label("n")).where(base, open_where)
                     .group_by(Finding.rule_id).order_by(func.count().desc(), Finding.rule_id).limit(10)).all()
    notes = ["Counts come from persisted findings. They describe patterns found by static rules, not confirmed defects.",
             "Open = new + existing + reopened. Suppressed findings are counted separately and are not hidden from the total.",
             "No composite quality score is computed."]
    if latest is not None and latest.rules_disabled and latest.config_source == "repository":
        notes.append(f"{latest.rules_disabled} rule(s) were disabled by the repository configuration in the latest run.")
    if latest is not None and latest.files_excluded:
        notes.append(f"{latest.files_excluded} file(s) were excluded in the latest run; they are not reported as clean.")
    return Summary(
        analyzed=latest is not None, latest_run=RunOut.model_validate(latest) if latest else None,
        total_findings=sum(by_status.values()), open=sum(by_status.get(s, 0) for s in OPEN_STATUSES),
        by_severity=group(Finding.severity, open_where), by_category=group(Finding.category, open_where),
        by_status=by_status, resolved_by_reason=group(Finding.resolution_reason, Finding.status == "resolved"),
        top_rules=[CountItem(key=k, count=int(n)) for k, n in top],
        files_analyzed=(latest.files_analyzed + latest.files_unchanged) if latest else 0,
        analyzer_errors=len(latest.analyzer_errors) if latest else 0, warnings=len(latest.warnings) if latest else 0,
        notes=notes)


@router.get("/analysis/rules", response_model=RulesOut)
def analysis_rules(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> RulesOut:
    repo = svc.get_repository(db, ctx, repository_id)
    eff, cstate = _config_state(repo.root_path, settings)
    rules = []
    for a in catalog.ANALYZERS:
        for r in a.rules:
            s = r.spec
            e = eff.rules[s.id]
            rules.append(RuleOut(
                id=s.id, title=s.title, description=s.description, category=s.category, severity=s.severity,
                effective_severity=e.severity or s.severity, enabled=e.enabled, languages=list(s.languages),
                version=s.version, analyzer_id=a.id, remediation=s.remediation, limitations=s.limitations,
                confidence=s.confidence, support=s.support, references=list(s.references),
                options=[OptionOut(name=o.name, default=o.default, effective=e.options.get(o.name, o.default),
                                   minimum=o.minimum, maximum=o.maximum, description=o.description) for o in s.options]))
    return RulesOut(
        rules=rules, config=cstate,
        analyzers=[AnalyzerOut(id=a.id, name=a.name, version=a.version, languages=list(a.languages), rules=len(a.rules))
                   for a in catalog.ANALYZERS],
        languages=[LanguageSupport(language="Python", status="implemented",
                                   note="Standard-library AST analysis; see each rule's support level and limitations."),
                   LanguageSupport(language="JavaScript / TypeScript", status="unsupported",
                                   note="Parsed for symbols and imports by the indexer, but no analysis rules exist yet."),
                   LanguageSupport(language="Other", status="unsupported", note="Metadata only.")])


@router.get("/analysis/config", response_model=ConfigOut)
def analysis_config(repository_id: str, ctx: RequestContext = Depends(get_context), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> ConfigOut:
    repo = svc.get_repository(db, ctx, repository_id)
    eff, cstate = _config_state(repo.root_path, settings)
    defaults = cfg_mod.defaults()
    return ConfigOut(
        path=cfg_mod.CONFIG_PATH, state=cstate, exclude=eff.exclude, exclude_generated=eff.exclude_generated,
        disabled_rules=sorted(r for r, v in eff.rules.items() if not v.enabled),
        severity_overrides={r: v.severity for r, v in eff.rules.items() if v.severity},
        option_overrides={r: {k: val for k, val in v.options.items() if val != defaults.rules[r].options.get(k)}
                          for r, v in eff.rules.items() if v.options != defaults.rules[r].options},
        server_limits={"max_files": settings.analysis_max_files, "max_file_bytes": settings.analysis_max_file_bytes,
                       "time_budget_seconds": settings.analysis_time_budget_seconds,
                       "max_findings_per_file": settings.analysis_max_findings_per_file,
                       "max_concurrent_runs": settings.analysis_max_concurrent_runs},
        note=LITERAL_NOTE)
_ = (CATEGORIES, STATUSES, utcnow)
