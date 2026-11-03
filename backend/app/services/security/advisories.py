"""Pluggable, source-attributed advisory providers and ecosystem-aware matching.

Providers: ``workspace-store`` (advisories previously imported or cached for this workspace), ``fixtures`` (a directory of OSV-format JSON files, for offline
operation) and ``osv`` (api.osv.dev; OFF unless ``NOVA_SECURITY_OSV_ENABLED``; live behaviour is unverified). A provider that cannot answer is recorded as such
and never counted as "no vulnerabilities".
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.models import SecAdvisory
from app.services.analysis.redaction import scrub
from app.services.cicd import providers as cip
from app.services.cicd.errors import ProviderError
from app.services.security import versions
from app.services.security.inventory import norm_name

ECOSYSTEMS = ("PyPI", "npm")
_SEV = {"LOW": "low", "MODERATE": "medium", "MEDIUM": "medium", "HIGH": "high", "CRITICAL": "critical"}


@dataclass
class AdvisoryRec:
    provider: str
    advisory_id: str
    ecosystem: str
    package: str
    aliases: list[str] = field(default_factory=list)
    summary: str = ""
    ranges: list[dict[str, Any]] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    severity: str | None = None
    score: str | None = None
    scoring_system: str | None = None
    references: list[str] = field(default_factory=list)
    published: str | None = None
    modified: str | None = None
    retrieved_at: datetime | None = None
    uncertainty: list[str] = field(default_factory=list)
    raw_sha256: str = ""

    def ids(self) -> set[str]:
        return {self.advisory_id, *self.aliases}


@dataclass
class ProviderResult:
    name: str
    state: str  # ok | unavailable | error | disabled
    detail: str = ""
    records: list[AdvisoryRec] = field(default_factory=list)
    queried: int = 0


class AdvisoryProvider(Protocol):
    name: str

    def fetch(self, packages: list[tuple[str, str]]) -> ProviderResult: ...


# ------------------------------------------------------------------------------------------------ OSV normalisation
def normalize_osv(doc: Any, provider: str, retrieved: datetime) -> list[AdvisoryRec]:
    """One record per affected package of a supported ecosystem. Malformed documents raise ValueError."""
    if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not doc["id"] or len(doc["id"]) > 100:
        raise ValueError("an advisory needs a string id")
    raw = hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()
    aliases = [a for a in (doc.get("aliases") or []) if isinstance(a, str)][:30]
    summary = scrub(str(doc.get("summary") or (doc.get("details") or "")[:300]), 500)
    refs = [r["url"] for r in (doc.get("references") or []) if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].startswith(("https://", "http://"))][:20]
    sev_text = ((doc.get("database_specific") or {}).get("severity") if isinstance(doc.get("database_specific"), dict) else None)
    severity = _SEV.get(str(sev_text).upper()) if sev_text else None
    score = scoring = None
    for s in doc.get("severity") or []:
        if isinstance(s, dict) and isinstance(s.get("score"), str):
            score, scoring = s["score"][:200], str(s.get("type") or "")[:20] or None
            break
    out: list[AdvisoryRec] = []
    for aff in doc.get("affected") or []:
        pkg = aff.get("package") if isinstance(aff, dict) else None
        if not isinstance(pkg, dict) or pkg.get("ecosystem") not in ECOSYSTEMS or not isinstance(pkg.get("name"), str):
            continue
        eco = pkg["ecosystem"]
        unc: list[str] = []
        ranges: list[dict[str, Any]] = []
        fixed: list[str] = []
        for r in aff.get("ranges") or []:
            if not isinstance(r, dict):
                continue
            if r.get("type") not in ("SEMVER", "ECOSYSTEM"):
                unc.append(f"range type {r.get('type')} is not evaluated")
                continue
            evs = [e for e in (r.get("events") or []) if isinstance(e, dict)]
            ranges.append({"type": r["type"], "events": evs})
            fixed.extend(str(e["fixed"]) for e in evs if "fixed" in e)
        vers = [v for v in (aff.get("versions") or []) if isinstance(v, str)][:500]
        if not ranges and not vers:
            unc.append("no affected versions are given")
        if severity is None:
            unc.append("severity not provided by the source" + (f" (only a {scoring} vector is given and is not scored)" if score else ""))
        out.append(AdvisoryRec(provider, doc["id"], eco, norm_name(eco, pkg["name"]), aliases, summary, ranges, vers, sorted(set(fixed)), severity, score, scoring, refs,
                               str(doc["published"])[:40] if doc.get("published") else None, str(doc["modified"])[:40] if doc.get("modified") else None, retrieved, unc, raw))
    return out


# ------------------------------------------------------------------------------------------------ providers
class StoreProvider:
    name = "workspace-store"

    def __init__(self, db: Session, workspace_id: str) -> None:
        self.db, self.workspace_id = db, workspace_id

    def fetch(self, packages: list[tuple[str, str]]) -> ProviderResult:
        recs: list[AdvisoryRec] = []
        for eco in sorted({e for e, _ in packages}):
            names = sorted({n for e, n in packages if e == eco})
            for i in range(0, len(names), 400):
                for r in self.db.scalars(select(SecAdvisory).where(SecAdvisory.workspace_id == self.workspace_id, SecAdvisory.ecosystem == eco,
                                                                   SecAdvisory.package.in_(names[i:i + 400]))):
                    recs.append(AdvisoryRec(r.provider, r.advisory_id, r.ecosystem, r.package, list(r.aliases or []), r.summary, list(r.ranges or []), list(r.versions or []),
                                            list(r.fixed or []), r.severity, r.score, r.scoring_system, list(r.references or []), r.published, r.modified, r.retrieved_at,
                                            list(r.uncertainty or []), r.raw_sha256))
        return ProviderResult(self.name, "ok", f"{len(recs)} cached advisory record(s)", recs, len(packages))


class DirProvider:
    def __init__(self, directory: str, name: str | None = None) -> None:
        self.directory = Path(directory)
        self.name = name or f"fixtures:{self.directory.name}"

    def fetch(self, packages: list[tuple[str, str]]) -> ProviderResult:
        if not self.directory.is_dir():
            return ProviderResult(self.name, "unavailable", "advisory directory does not exist")
        wanted = set(packages)
        recs: list[AdvisoryRec] = []
        bad = 0
        now = utcnow()
        for f in sorted(self.directory.glob("*.json"))[:5000]:
            try:
                if f.is_symlink() or f.stat().st_size > 1_000_000:
                    bad += 1
                    continue
                docs = json.loads(f.read_text("utf-8"))
                for d in docs if isinstance(docs, list) else [docs]:
                    recs.extend(r for r in normalize_osv(d, self.name, now) if (r.ecosystem, r.package) in wanted)
            except (OSError, ValueError, RecursionError):
                bad += 1
        return ProviderResult(self.name, "ok" if not bad else "ok", f"{len(recs)} record(s)" + (f"; {bad} unreadable file(s) skipped" if bad else ""), recs, len(packages))


class OsvProvider:
    name = "osv"

    def __init__(self, settings: Settings, transport: cip.Transport | None = None) -> None:
        self.settings = settings
        self.transport = transport or cip.HttpTransport(settings.security_osv_base)

    def fetch(self, packages: list[tuple[str, str]]) -> ProviderResult:
        if not self.settings.security_osv_enabled:
            return ProviderResult(self.name, "disabled", "live advisory lookups are disabled (set NOVA_SECURITY_OSV_ENABLED=true to enable)")
        recs: list[AdvisoryRec] = []
        now = utcnow()
        for eco, name in packages[:2000]:
            token: str | None = None
            for _ in range(5):
                body = {"package": {"name": name, "ecosystem": eco}}
                if token:
                    body["page_token"] = token
                try:
                    resp = self.transport.request("POST", self.settings.security_osv_base.rstrip("/") + "/v1/query", {"Content-Type": "application/json", "Accept": "application/json"},
                                                  json.dumps(body).encode(), self.settings.security_http_timeout_seconds, self.settings.security_http_max_response_bytes)
                except ProviderError as exc:
                    return ProviderResult(self.name, "unavailable", f"{exc.error_class}: {scrub(exc.message, 200)}", recs, len(packages))
                if resp.status != 200:
                    return ProviderResult(self.name, "error", f"HTTP {resp.status}", recs, len(packages))
                try:
                    data = json.loads(resp.body.decode("utf-8"))
                    for d in data.get("vulns") or []:
                        recs.extend(r for r in normalize_osv(d, self.name, now) if r.ecosystem == eco and r.package == norm_name(eco, name))
                except (ValueError, AttributeError, RecursionError):
                    return ProviderResult(self.name, "error", "malformed response", recs, len(packages))
                token = data.get("next_page_token") if isinstance(data, dict) else None
                if not token:
                    break
        return ProviderResult(self.name, "ok", f"{len(recs)} record(s)", recs, len(packages))


def configured_providers(db: Session, settings: Settings, workspace_id: str, osv_transport: cip.Transport | None = None) -> list[AdvisoryProvider]:
    out: list[AdvisoryProvider] = [StoreProvider(db, workspace_id)]
    if settings.security_advisory_dir:
        out.append(DirProvider(settings.security_advisory_dir))
    out.append(OsvProvider(settings, osv_transport))
    return out


def persist(db: Session, workspace_id: str, recs: list[AdvisoryRec]) -> int:
    """Upsert provider-sourced records into the workspace cache (never from the workspace-store itself)."""
    n = 0
    for r in recs:
        row = db.scalar(select(SecAdvisory).where(SecAdvisory.workspace_id == workspace_id, SecAdvisory.provider == r.provider, SecAdvisory.advisory_id == r.advisory_id,
                                                  SecAdvisory.ecosystem == r.ecosystem, SecAdvisory.package == r.package))
        if row is None:
            row = SecAdvisory(workspace_id=workspace_id, provider=r.provider, advisory_id=r.advisory_id, ecosystem=r.ecosystem, package=r.package)
            db.add(row)
        row.aliases, row.summary, row.ranges, row.versions, row.fixed = r.aliases, r.summary, r.ranges, r.versions, r.fixed
        row.severity, row.score, row.scoring_system, row.references = r.severity, r.score, r.scoring_system, r.references
        row.published, row.modified, row.retrieved_at, row.uncertainty, row.raw_sha256 = r.published, r.modified, r.retrieved_at or utcnow(), r.uncertainty, r.raw_sha256
        n += 1
    return n


# ------------------------------------------------------------------------------------------------ matching
@dataclass
class Match:
    ecosystem: str
    name: str
    version: str | None
    status: str  # confirmed | possible
    ids: list[str]
    records: list[AdvisoryRec]
    reason: str

    def severity(self) -> str | None:
        order = ["critical", "high", "medium", "low"]
        sev = [r.severity for r in self.records if r.severity in order]
        return min(sev, key=order.index) if sev else None

    def fixed(self) -> list[str]:
        return sorted({f for r in self.records for f in r.fixed}, key=lambda v: (versions.parse(self.ecosystem, v) is None, versions.parse(self.ecosystem, v) or 0))  # type: ignore[arg-type, return-value]


def _groups(recs: list[AdvisoryRec]) -> list[list[AdvisoryRec]]:
    parent = list(range(len(recs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict[str, int] = {}
    for i, r in enumerate(recs):
        for ident in r.ids():
            if ident in owner:
                parent[find(i)] = find(owner[ident])
            else:
                owner[ident] = i
    groups: dict[int, list[AdvisoryRec]] = {}
    for i, r in enumerate(recs):
        groups.setdefault(find(i), []).append(r)
    return [sorted(g, key=lambda r: (r.provider, r.advisory_id)) for _, g in sorted(groups.items())]


def match(components: list[Any], results: list[ProviderResult]) -> tuple[list[Match], dict[tuple[str, str, str], dict[str, Any]]]:
    """components: objects with ecosystem/name/version. Returns (matches, per-component assessment keyed by (eco, name, version or ''))."""
    ok = [r for r in results if r.state == "ok"]
    by_pkg: dict[tuple[str, str], list[AdvisoryRec]] = {}
    for r in ok:
        for rec in r.records:
            by_pkg.setdefault((rec.ecosystem, rec.package), []).append(rec)
    matches: list[Match] = []
    assessments: dict[tuple[str, str, str], dict[str, Any]] = {}
    queried = [r.name for r in ok]
    for c in components:
        key = (c.ecosystem, c.name, c.version or "")
        if c.ecosystem not in ECOSYSTEMS:
            assessments[key] = {"status": "unassessed", "reason": f"ecosystem {c.ecosystem} is not supported", "sources": []}
            continue
        if not ok:
            assessments[key] = {"status": "unassessed", "reason": "no advisory source was able to answer", "sources": []}
            continue
        recs = by_pkg.get((c.ecosystem, c.name), [])
        parsed = versions.parse(c.ecosystem, c.version)
        if parsed is None:
            why = "no version is pinned" if not c.version else "the version is not a valid " + ("PEP 440" if c.ecosystem == "PyPI" else "semantic") + " version"
            for g in _groups(recs):
                matches.append(Match(c.ecosystem, c.name, c.version, "possible", sorted({i for r in g for i in r.ids()}), g, f"{why}; cannot tell whether this advisory applies"))
            assessments[key] = {"status": "possible" if recs else "unassessed", "reason": why + ("" if recs else "; a vulnerable version cannot be excluded"), "sources": queried}
            continue
        found = False
        for g in _groups(recs):
            verdicts = [versions.affected(c.ecosystem, c.version, r.ranges, r.versions) for r in g]
            if any(v is True for v in verdicts):
                matches.append(Match(c.ecosystem, c.name, c.version, "confirmed", sorted({i for r in g for i in r.ids()}), g, f"version {c.version} is inside an affected range"))
                found = True
            elif any(v is None for v in verdicts):
                matches.append(Match(c.ecosystem, c.name, c.version, "possible", sorted({i for r in g for i in r.ids()}), g, "the source gives no evaluable affected-version data"))
                found = True
        assessments[key] = {"status": "affected" if found else "no_known_advisory", "sources": queried,
                            "reason": "" if found else "no advisory in the queried sources affects this version; this is not a statement that the package is safe"}
    return matches, assessments


def summarize_providers(results: list[ProviderResult]) -> list[dict[str, Any]]:
    return [{"name": r.name, "state": r.state, "detail": r.detail, "records": len(r.records), "queried": r.queried} for r in results]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def provider_status(settings: Settings) -> list[dict[str, Any]]:
    """Which advisory sources are configured. Nothing is contacted."""
    return [
        {"name": "workspace-store", "enabled": True, "kind": "local", "detail": "advisories cached or imported into this workspace"},
        {"name": "fixtures", "enabled": bool(settings.security_advisory_dir), "kind": "offline", "detail": "directory of OSV JSON files" if settings.security_advisory_dir else "not configured (NOVA_SECURITY_ADVISORY_DIR)"},
        {"name": "osv", "enabled": bool(settings.security_osv_enabled), "kind": "network", "detail": "queries the OSV API for package names and versions only" if settings.security_osv_enabled else "disabled (NOVA_SECURITY_OSV_ENABLED)"},
    ]
