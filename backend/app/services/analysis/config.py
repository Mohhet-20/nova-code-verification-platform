"""Repository analysis configuration: strict parsing, validation against the rule catalog, and exclusion matching.

Security stance: the file lives in the (untrusted) repository, so it can only *narrow or tune* what is analysed. It cannot
raise file-size, time, count or concurrency limits, cannot affect authorization, and every effect it has (rules disabled,
files excluded) is reported back in the run summary so a configuration cannot silently produce a clean-looking result.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pathspec
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.errors import NovaError
from app.security.paths import safe_join
from app.services.analysis.catalog import rule_index
from app.services.analysis.model import SEVERITIES

CONFIG_PATH = ".nova/analysis.json"
MAX_CONFIG_BYTES = 65_536
LANGUAGE_KEYS = ("python",)


class InvalidAnalysisConfig(NovaError):
    status_code = 422
    code = "invalid_analysis_config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleOverride(_Strict):
    enabled: bool | None = None
    severity: Literal["critical", "high", "medium", "low", "info"] | None = None
    options: dict[str, int | float | bool] = Field(default_factory=dict, max_length=20)


class LanguageConfig(_Strict):
    rules: dict[str, RuleOverride] = Field(default_factory=dict, max_length=200)


class AnalysisConfigFile(_Strict):
    version: Literal[1] = 1
    exclude: list[str] = Field(default_factory=list, max_length=100)
    exclude_generated: bool = True
    rules: dict[str, RuleOverride] = Field(default_factory=dict, max_length=200)
    languages: dict[str, LanguageConfig] = Field(default_factory=dict, max_length=10)

    @field_validator("exclude")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for p in value:
            if not p.strip() or len(p) > 200 or "\x00" in p or p.startswith("/") or ".." in p.replace("\\", "/").split("/"):
                raise ValueError("exclude patterns must be non-empty, at most 200 characters, relative, and contain no '..'")
        return value


@dataclass
class EffectiveRule:
    enabled: bool
    severity: str | None  # None = rule default
    options: dict[str, Any]


@dataclass
class EffectiveConfig:
    rules: dict[str, EffectiveRule]
    exclude: list[str]
    exclude_generated: bool
    source: str  # "defaults" | "repository"
    errors: list[str] = field(default_factory=list)

    @property
    def spec(self) -> pathspec.GitIgnoreSpec:
        return pathspec.GitIgnoreSpec.from_lines(self.exclude)

    def is_excluded(self, rel_path: str) -> bool:
        return bool(self.exclude) and self.spec.match_file(rel_path)

    @property
    def digest(self) -> str:
        payload = {"r": {k: [v.enabled, v.severity, sorted(v.options.items())] for k, v in sorted(self.rules.items())},
                   "x": self.exclude, "g": self.exclude_generated}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def enabled_options(self, analyzer_rule_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {rid: self.rules[rid].options for rid in analyzer_rule_ids if self.rules[rid].enabled}


def defaults() -> EffectiveConfig:
    rules = {}
    for rid, (_, spec) in rule_index().items():
        rules[rid] = EffectiveRule(True, None, {o.name: o.default for o in spec.options})
    return EffectiveConfig(rules, [], True, "defaults")


def read_raw(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Read ``.nova/analysis.json`` safely. Returns (data or None when absent, errors)."""
    try:
        path = safe_join(root, CONFIG_PATH)
        st = os.lstat(path)
    except FileNotFoundError:
        return None, []
    except (OSError, NovaError):
        return None, [f"{CONFIG_PATH} could not be read"]
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, [f"{CONFIG_PATH} must be a regular file (symbolic links are not followed)"]
    if st.st_size > MAX_CONFIG_BYTES:
        return None, [f"{CONFIG_PATH} is larger than {MAX_CONFIG_BYTES} bytes"]
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(MAX_CONFIG_BYTES + 1).decode("utf-8-sig"))
    except UnicodeDecodeError:
        return None, [f"{CONFIG_PATH} is not valid UTF-8"]
    except json.JSONDecodeError as exc:
        return None, [f"{CONFIG_PATH} is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"]
    except OSError:
        return None, [f"{CONFIG_PATH} could not be read"]
    if not isinstance(data, dict):
        return None, [f"{CONFIG_PATH} must contain a JSON object"]
    return data, []


def resolve(data: dict[str, Any] | None) -> EffectiveConfig:
    """Validate ``data`` strictly against the rule catalog. Raises InvalidAnalysisConfig listing every problem."""
    cfg = defaults()
    if data is None:
        return cfg
    errors: list[str] = []
    try:
        parsed = AnalysisConfigFile.model_validate(data)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]
        raise InvalidAnalysisConfig("The analysis configuration is invalid.", msgs[:30]) from None
    index = rule_index()
    layers: list[tuple[str, dict[str, RuleOverride]]] = [("rules", parsed.rules)]
    for lang, lc in parsed.languages.items():
        if lang not in LANGUAGE_KEYS:
            errors.append(f"languages.{lang}: unsupported language (supported: {', '.join(LANGUAGE_KEYS)})")
        layers.append((f"languages.{lang}.rules", lc.rules))
    for prefix, overrides in layers:
        for rid, ov in overrides.items():
            if rid not in index:
                errors.append(f"{prefix}.{rid}: unknown rule id")
                continue
            spec = index[rid][1]
            eff = cfg.rules[rid]
            if ov.enabled is not None:
                eff.enabled = ov.enabled
            if ov.severity is not None:
                if ov.severity not in SEVERITIES:
                    errors.append(f"{prefix}.{rid}.severity: invalid")
                eff.severity = ov.severity
            known = {o.name: o for o in spec.options}
            for name, value in ov.options.items():
                opt = known.get(name)
                if opt is None:
                    errors.append(f"{prefix}.{rid}.options.{name}: unknown option (known: {', '.join(known) or 'none'})")
                    continue
                if isinstance(opt.default, bool) != isinstance(value, bool):
                    errors.append(f"{prefix}.{rid}.options.{name}: expected {type(opt.default).__name__}")
                    continue
                if not isinstance(value, bool) and ((opt.minimum is not None and value < opt.minimum) or
                                                    (opt.maximum is not None and value > opt.maximum)):
                    errors.append(f"{prefix}.{rid}.options.{name}: must be between {opt.minimum} and {opt.maximum}")
                    continue
                eff.options[name] = value
    if errors:
        raise InvalidAnalysisConfig("The analysis configuration is invalid.", errors[:30])
    cfg.exclude = [p.strip() for p in parsed.exclude]
    cfg.exclude_generated = parsed.exclude_generated
    cfg.source = "repository"
    return cfg


def load_for_repository(root: Path) -> EffectiveConfig:
    data, errors = read_raw(root)
    if errors:
        raise InvalidAnalysisConfig("The analysis configuration is invalid.", errors)
    return resolve(data)


_GENERATED_MARKERS = ("@generated", "code generated", "do not edit", "auto-generated", "autogenerated", "generated by")


def looks_generated(rel_path: str, head: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    if name.endswith(("_pb2.py", "_pb2_grpc.py")):
        return True
    first = "\n".join(head.splitlines()[:5]).lower()
    return any(m in first for m in _GENERATED_MARKERS)
