"""Declared-dependency names read from bounded manifest contents already read during discovery.

Nothing is executed and no package manager is invoked; manifests are parsed as data.
"""
from __future__ import annotations

import json
import re
import tomllib

MANIFEST_NAMES = {"package.json", "pyproject.toml", "requirements.txt"}
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_py(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def is_manifest(rel_path: str) -> bool:
    base = rel_path.rsplit("/", 1)[-1]
    return base in MANIFEST_NAMES or (base.startswith("requirements") and base.endswith(".txt"))


def python_dependencies(rel_path: str, data: bytes) -> set[str]:
    base = rel_path.rsplit("/", 1)[-1]
    names: set[str] = set()
    try:
        text = data.decode("utf-8-sig")
        if base.endswith(".txt"):
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith(("-", "git+", "http")):
                    continue
                m = _REQ_NAME.match(line)
                if m:
                    names.add(normalize_py(m.group(1)))
        elif base == "pyproject.toml":
            doc = tomllib.loads(text)
            project = doc.get("project", {})
            reqs = list(project.get("dependencies", []))
            for group in project.get("optional-dependencies", {}).values():
                reqs.extend(group)
            for group in doc.get("dependency-groups", {}).values():
                reqs.extend(r for r in group if isinstance(r, str))
            for r in reqs:
                m = _REQ_NAME.match(r)
                if m:
                    names.add(normalize_py(m.group(1)))
            poetry = doc.get("tool", {}).get("poetry", {})
            for section in ("dependencies", "dev-dependencies"):
                names.update(normalize_py(k) for k in poetry.get(section, {}) if k.lower() != "python")
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        return names
    return names


def javascript_dependencies(data: bytes) -> set[str]:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(doc, dict):
        return set()
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = doc.get(section)
        if isinstance(value, dict):
            names.update(str(k) for k in value)
    return names
