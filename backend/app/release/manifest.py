"""Release manifest and CycloneDX SBOM, built only from facts available at build time. Nothing is signed; no attestation is claimed."""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from app.core.config import APP_VERSION, BACKEND_DIR
from app.release import dbtools

REPO_ROOT = BACKEND_DIR.parent


def source_revision() -> dict[str, Any]:
    rev = os.environ.get("NOVA_SOURCE_REVISION") or os.environ.get("GITHUB_SHA")
    if rev:
        return {"revision": rev, "source": "environment"}
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True).stdout.strip()  # noqa: S607
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10).stdout.strip())  # noqa: S607
        return {"revision": out, "source": "git", "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"revision": "unknown", "source": "unavailable (not a git checkout and NOVA_SOURCE_REVISION unset)"}


def _requirements(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?==([^\s;#]+)", line.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def backend_dependencies() -> list[dict[str, str]]:
    deps = []
    for name, pinned in _requirements(BACKEND_DIR / "requirements.txt"):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = ""
        deps.append({"name": name, "pinned": pinned, "installed": installed})
    return deps


def frontend_dependencies() -> list[dict[str, str]]:
    lock = REPO_ROOT / "frontend" / "package-lock.json"
    if not lock.is_file():
        return []
    data = json.loads(lock.read_text(encoding="utf-8"))
    return [{"name": k.split("node_modules/")[-1], "version": v.get("version", "")} for k, v in sorted(data.get("packages", {}).items()) if k and v.get("version")]


def sbom() -> dict[str, Any]:
    comps = [{"type": "library", "name": d["name"], "version": d["pinned"], "purl": f"pkg:pypi/{d['name'].lower()}@{d['pinned']}"} for d in backend_dependencies()]
    comps += [{"type": "library", "name": d["name"], "version": d["version"], "purl": f"pkg:npm/{d['name']}@{d['version']}"} for d in frontend_dependencies()]
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"timestamp": datetime.now(UTC).isoformat(), "component": {"type": "application", "name": "nova", "version": APP_VERSION}, "tools": [{"name": "nova app.release manifest"}]},
            "components": comps}


def test_summary(junit: Path | None) -> dict[str, Any]:
    if junit is None or not junit.is_file():
        return {"available": False, "note": "no junit report supplied; tests are not claimed"}
    import xml.etree.ElementTree as ET  # noqa: S405 - local trusted CI output
    root = ET.parse(junit).getroot()  # noqa: S314
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
    return {"available": True, **total}


def build(junit: Path | None = None, image: dict[str, str] | None = None) -> dict[str, Any]:
    classes = dbtools.classify_migrations()
    return {
        "format": 1, "name": "nova", "version": APP_VERSION, "built_at": datetime.now(UTC).isoformat(), "source": source_revision(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "migrations": {"head": dbtools.head_revision(), "revisions": classes, "note": "'additive' is a heuristic; non-additive revisions need review before relying on schema-forward rollback"},
        "backend_dependencies": backend_dependencies(), "frontend_dependency_count": len(frontend_dependencies()),
        "tests": test_summary(junit), "image": image or {"built": False, "note": "no container image was built for this manifest"},
        "signature": None, "attestation": None,
    }
