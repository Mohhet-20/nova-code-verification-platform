"""Run pytest (optionally under coverage.py) in a *restricted subprocess*.

This is NOT a sandbox. What it does: refuses elevated privileges, starts a fresh process group, passes a minimal
environment (no inherited secrets), applies POSIX resource limits, enforces a wall-clock timeout, bounds captured output
and keeps all artifacts under the artifact root. What it does not do: filesystem isolation (the test can read and write
anything the NOVA user can), network isolation, or protection from a hostile test. Tests are arbitrary Python.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET  # noqa: S405 - input is size-capped and DOCTYPE/ENTITY is rejected before parsing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.services.analysis.redaction import scrub

OMIT = ["*/tests/*", "*/test/*", "*/test_*.py", "*/*_test.py", "*/conftest.py", "*/.venv/*", "*/venv/*", "*/site-packages/*",
        "*/node_modules/*", "*/migrations/*", "*/__pycache__/*"]
LAUNCHER = (
    "import json,os,sys\n"
    "import resource\n"
    "lim=json.loads(sys.argv[1])\n"
    "for name,val in lim.items():\n"
    "    try:\n"
    "        resource.setrlimit(getattr(resource,name),(val,val))\n"
    "    except (ValueError,OSError):\n"
    "        pass\n"
    "os.execv(sys.argv[2],sys.argv[2:])\n"
)


@dataclass
class RunSpec:
    kind: str  # generated | existing
    repo_root: Path
    run_dir: Path
    cwd: Path
    rootdir: Path
    targets: list[str]  # arguments passed to pytest (absolute for generated, repo-relative for existing)
    import_roots: list[Path]
    coverage: bool
    coverage_include: list[Path] = field(default_factory=list)  # generated: exact target files
    coverage_source: Path | None = None  # existing: whole repository, minus OMIT
    pytest_ini: Path | None = None


@dataclass
class RunResult:
    status: str = "error"
    reason: str | None = None
    exit_code: int | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    command: list[str] = field(default_factory=list)
    environment_profile: dict[str, Any] = field(default_factory=dict)
    collected: int | None = None
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    coverage_status: str = "not_requested"
    coverage_error: str | None = None
    coverage_tool: str | None = None
    coverage_files: list[dict[str, Any]] = field(default_factory=list)
    coverage_totals: dict[str, Any] = field(default_factory=dict)
    unmeasured_targets: list[str] = field(default_factory=list)
    tool_versions: dict[str, Any] = field(default_factory=dict)


def is_elevated() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
    return os.geteuid() == 0


def test_python(settings: Settings) -> str:
    return settings.test_python or sys.executable


def minimal_env(run_dir: Path, import_roots: list[Path]) -> dict[str, str]:
    home, tmp = run_dir / "home", run_dir / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]) if os.name != "nt" else os.environ.get("PATH", ""),
        "HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join(str(p) for p in import_roots),
        "LANG": "C.UTF-8", "NO_COLOR": "1",
    }
    if os.name == "nt":
        for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT"):
            if k in os.environ:
                env[k] = os.environ[k]
    return env


def _probe(python: str, code: str, env: dict[str, str], cwd: Path) -> tuple[bool, str]:
    try:
        p = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=30, env=env, cwd=cwd,  # noqa: S603
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return p.returncode == 0, p.stdout.strip()


def _drain(stream: Any, sink: list[bytes], limit: int, flag: list[bool]) -> None:
    total = 0
    try:
        for chunk in iter(lambda: stream.read(8192), b""):
            room = limit - total
            if room > 0:
                sink.append(chunk[:room])
            if len(chunk) > max(room, 0):
                flag[0] = True
            total += len(chunk)
    except (OSError, ValueError):
        pass


def sanitize(text: str, subs: dict[str, str], limit: int) -> str:
    for src, dst in sorted(subs.items(), key=lambda kv: -len(kv[0])):
        if src:
            text = text.replace(src, dst)
    return scrub(text, limit)


def _write_rc(spec: RunSpec) -> Path:
    rc = spec.run_dir / "nova.coveragerc"
    lines = ["[run]", "branch = True", "relative_files = False"]
    if spec.coverage_include:
        lines += ["include ="] + [f"    {p}" for p in spec.coverage_include]
    elif spec.coverage_source is not None:
        lines += ["source =", f"    {spec.coverage_source}", "omit ="] + [f"    {o}" for o in OMIT]
    rc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rc


def parse_junit(path: Path, subs: dict[str, str]) -> tuple[dict[str, int] | None, list[dict[str, Any]]]:
    try:
        if not path.is_file() or path.stat().st_size > 4_000_000:
            return None, []
        raw = path.read_bytes()
    except OSError:
        return None, []
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        return None, []
    try:
        root = ET.fromstring(raw)  # noqa: S314
    except ET.ParseError:
        return None, []
    counts = {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        counts["collected"] += 1
        classname, name = case.get("classname", ""), case.get("name", "")
        fail, err, skip = case.find("failure"), case.find("error"), case.find("skipped")
        if fail is not None or err is not None:
            node = fail if fail is not None else err
            counts["failed" if fail is not None else "errors"] += 1
            if len(failures) < 50:
                failures.append({
                    "test": sanitize(f"{classname}::{name}".strip(":"), subs, 200), "classname": classname[:200], "name": name[:200],
                    "kind": "failure" if fail is not None else "error",
                    "message": sanitize((node.get("message") or "")[:400], subs, 300),  # type: ignore[union-attr]
                    "traceback": sanitize((node.text or "")[:6000], subs, 4000)})  # type: ignore[union-attr]
        elif skip is not None:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts, failures


def _ranges(lines: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def _rel(name: str, spec: RunSpec) -> str | None:
    root = spec.repo_root.resolve()
    try:
        p = Path(name) if os.path.isabs(name) else spec.cwd / name
        return str(p.resolve().relative_to(root)).replace("\\", "/")
    except (ValueError, OSError):
        return None


def _coverage_report(python: str, spec: RunSpec, rc: Path, data: Path, env: dict[str, str], res: RunResult,
                     subs: dict[str, str]) -> None:
    if not data.is_file():
        res.coverage_status = "no_data"
        res.coverage_error = "coverage recorded no data (the test process ended before coverage could save it)"
        return
    out = spec.run_dir / "coverage.json"
    try:
        p = subprocess.run([python, "-m", "coverage", "json", f"--rcfile={rc}", f"--data-file={data}", "-o", str(out),  # noqa: S603
                            "--ignore-errors"], capture_output=True, text=True, timeout=90, env=env, cwd=spec.cwd,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        res.coverage_status, res.coverage_error = "failed", "the coverage report command did not finish"
        return
    if p.returncode != 0 or not out.is_file():
        tail = sanitize((p.stderr or p.stdout)[-400:], subs, 300)
        res.coverage_status = "no_data" if "No data to report" in (p.stderr + p.stdout) else "failed"
        res.coverage_error = ("coverage measured nothing within the configured scope" if res.coverage_status == "no_data"
                              else f"coverage report failed: {tail}")
        return
    try:
        data_json = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        res.coverage_status, res.coverage_error = "failed", "coverage report could not be parsed"
        return
    files: list[dict[str, Any]] = []
    stm = cov = br = brc = 0
    for name, info in (data_json.get("files") or {}).items():
        rel = _rel(name, spec)
        if rel is None:
            continue  # outside the repository (e.g. site-packages) is never stored
        s = info.get("summary", {})
        n, c = int(s.get("num_statements", 0)), int(s.get("covered_lines", 0))
        nb, cb = s.get("num_branches"), s.get("covered_branches")
        files.append({"rel_path": rel, "statements": n, "covered": c, "line_percent": round(100.0 * c / n, 2) if n else 100.0,
                      "missing_lines": _ranges([int(x) for x in info.get("missing_lines", [])]),
                      "branches": int(nb) if nb is not None else None, "branches_covered": int(cb) if cb is not None else None})
        stm, cov = stm + n, cov + c
        if nb is not None:
            br, brc = br + int(nb), brc + int(cb or 0)
    if not files:
        res.coverage_status, res.coverage_error = "no_data", "no file inside the configured scope was measured"
        return
    files.sort(key=lambda f: f["rel_path"])
    res.coverage_files = files[:2000]
    res.coverage_status = "measured"
    res.coverage_totals = {"lines_total": stm, "lines_covered": cov, "line_percent": round(100.0 * cov / stm, 2) if stm else None,
                           "branches_total": br, "branches_covered": brc,
                           "branch_percent": round(100.0 * brc / br, 2) if br else None, "files": len(files)}
    if spec.coverage_include:
        have = {f["rel_path"] for f in files}
        wanted = [_rel(str(p), spec) for p in spec.coverage_include]
        res.unmeasured_targets = sorted(w for w in wanted if w and w not in have)


def execute(spec: RunSpec, settings: Settings) -> RunResult:
    res = RunResult()
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    python = test_python(settings)
    subs = {str(spec.run_dir): "<run>", str(spec.repo_root): "<repo>", python: "<test-python>"}
    limits = {"timeout_seconds": settings.test_timeout_seconds, "max_output_bytes": settings.test_max_output_bytes,
              "memory_mb": settings.test_memory_limit_mb if os.name != "nt" else None}
    res.environment_profile = {
        "mechanism": "restricted-subprocess", "sandbox": False, "network_isolated": False, "filesystem_isolated": False,
        "elevated_privileges_refused": True, "environment_inherited": False, "platform": sys.platform,
        "resource_limits_enforced": os.name != "nt", "process_group_kill": os.name != "nt", "limits": limits}
    if not settings.test_execution_enabled:
        res.status, res.reason = "rejected", "Running repository code is disabled on this server (NOVA_TEST_EXECUTION_ENABLED=false)."
        return res
    if is_elevated():
        res.status, res.reason = "rejected", "Refusing to run tests with administrator/root privileges."
        return res
    env = minimal_env(spec.run_dir, spec.import_roots)
    res.environment_profile["environment_keys"] = sorted(env)
    ok, out = _probe(python, "import sys;print(sys.version.split()[0]);import pytest;print(pytest.__version__)", env, spec.run_dir)
    if not ok:
        res.status, res.reason = "error", "pytest is not available to the configured test interpreter (install it: pip install pytest)."
        return res
    versions = out.splitlines()
    res.tool_versions = {"python": versions[0] if versions else None, "pytest": versions[1] if len(versions) > 1 else None}
    use_cov = spec.coverage
    if use_cov:
        cok, cout = _probe(python, "import coverage;print(coverage.__version__)", env, spec.run_dir)
        if cok:
            res.tool_versions["coverage.py"] = cout
            res.coverage_tool = f"coverage.py {cout}"
        else:
            use_cov = False
            res.coverage_status = "tool_unavailable"
            res.coverage_error = "coverage.py is not installed for the configured test interpreter (pip install coverage)."
    junit = spec.run_dir / "junit.xml"
    data = spec.run_dir / ".coverage"
    rc = _write_rc(spec) if use_cov else spec.run_dir / "nova.coveragerc"
    pytest_args = ["-p", "no:cacheprovider", "-q", "--no-header", f"--junitxml={junit}", f"--rootdir={spec.rootdir}"]
    if spec.pytest_ini is not None:
        pytest_args += ["-c", str(spec.pytest_ini)]
    pytest_args += spec.targets
    argv = ([python, "-m", "coverage", "run", f"--rcfile={rc}", f"--data-file={data}", "-m", "pytest", *pytest_args]
            if use_cov else [python, "-m", "pytest", *pytest_args])
    res.command = [("<staged>/" + os.path.basename(a) if os.path.isabs(a) and a not in subs else sanitize(a, subs, 300))
                   for a in argv]
    popen_argv, kwargs = argv, {}
    if os.name != "nt":
        rl: dict[str, int] = {"RLIMIT_CPU": int(settings.test_timeout_seconds) + 5, "RLIMIT_CORE": 0,
                              "RLIMIT_NOFILE": 512, "RLIMIT_FSIZE": 50_000_000,
                              "RLIMIT_AS": settings.test_memory_limit_mb * 1024 * 1024}
        popen_argv = [python, "-c", LAUNCHER, json.dumps(rl), *argv]
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    out_b: list[bytes] = []
    err_b: list[bytes] = []
    flag = [False]
    res.started_at = time.time()
    try:
        proc = subprocess.Popen(popen_argv, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # noqa: S603
                                stderr=subprocess.PIPE, **kwargs)
    except OSError:
        res.status, res.reason = "error", "the test process could not be started"
        res.finished_at = time.time()
        return res
    threads = [threading.Thread(target=_drain, args=(proc.stdout, out_b, settings.test_max_output_bytes, flag), daemon=True),
               threading.Thread(target=_drain, args=(proc.stderr, err_b, settings.test_max_output_bytes, flag), daemon=True)]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=settings.test_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait()
    for t in threads:
        t.join(timeout=5)
    res.finished_at = time.time()
    res.exit_code = proc.returncode
    res.truncated = flag[0]
    res.stdout = sanitize(b"".join(out_b).decode("utf-8", errors="replace"), subs, settings.test_max_output_bytes)
    res.stderr = sanitize(b"".join(err_b).decode("utf-8", errors="replace"), subs, settings.test_max_output_bytes)
    counts, failures = parse_junit(junit, subs)
    if counts is not None:
        res.collected, res.passed, res.failed = counts["collected"], counts["passed"], counts["failed"]
        res.errors, res.skipped, res.failures = counts["errors"], counts["skipped"], failures
    if timed_out:
        res.status, res.reason = "timed_out", f"Stopped after {settings.test_timeout_seconds:g} s."
    else:
        code = proc.returncode
        mapping = {0: ("passed", None), 1: ("failed", None), 2: ("error", "Test execution was interrupted or hit a collection error."),
                   3: ("error", "pytest hit an internal error."), 4: ("error", "pytest was invoked incorrectly."),
                   5: ("error", "pytest collected no tests.")}
        res.status, res.reason = mapping.get(code if code is not None and code >= 0 else 3,
                                             ("error", f"the test process was terminated (exit code {code})"))
        if code == 0 and res.passed == 0 and (res.skipped or 0) > 0:
            res.reason = "No test asserted anything: every collected test was skipped."
    if use_cov:
        _coverage_report(python, spec, rc, data, env, res, subs)
    return res
