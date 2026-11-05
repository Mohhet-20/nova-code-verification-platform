"""Security rules that use light intraprocedural *taint tracking*.

A value is "tainted" when it comes from an obvious untrusted source (request objects, ``input()``, ``sys.argv``, parameters of web-route handlers) and flows
through assignments, string formatting, concatenation and ordinary calls into a sensitive call. This is a conservative, single-function approximation:
it cannot follow flows across functions, containers or attributes, so *absence of a finding is not evidence of safety*. Nothing is executed.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from app.services.analysis.analyzers.py_facts import FileFacts, loc
from app.services.analysis.model import Analyzer, RawFinding, Rule, RuleContext, RuleSpec

PY = ("Python",)
RULES: list[Rule] = []


def rule(spec: RuleSpec):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        RULES.append(Rule(spec, fn))
        return fn

    return deco


_REQ_ATTRS = {"args", "form", "json", "values", "files", "data", "cookies", "headers", "GET", "POST", "query_params", "path_params", "body", "get_json", "view_args"}
_ROUTE_DECOS = {"route", "get", "post", "put", "delete", "patch", "websocket", "api_route", "head", "options"}
_SANITIZERS = {"int", "float", "bool", "len", "uuid.UUID", "os.path.basename", "werkzeug.utils.secure_filename", "secure_filename", "shlex.quote", "pipes.quote",
               "urllib.parse.quote", "urllib.parse.quote_plus", "html.escape", "re.escape", "ipaddress.ip_address", "ipaddress.ip_network", "pathlib.PurePath.name"}


def _is_source(node: ast.AST, facts: FileFacts) -> str | None:
    if isinstance(node, ast.Call):
        n = facts.resolve(node.func)
        if n in ("input", "builtins.input"):
            return "input()"
        if n == "sys.stdin.read" or n == "sys.stdin.readline":
            return "stdin"
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute | ast.Name):
            base = facts.resolve(node.func.value)
            if base and re.match(r"^(flask\.)?request(\.|$)", base) and node.func.attr in ("get_json", "get", "getlist", "get_data"):
                return "request data"
    if isinstance(node, ast.Attribute):
        n = facts.resolve(node)
        if n == "sys.argv":
            return "sys.argv"
        if n and re.match(r"^(flask\.)?request\.", n) and node.attr in _REQ_ATTRS:
            return "request data"
        if isinstance(node.value, ast.Attribute) or isinstance(node.value, ast.Name):
            base = facts.resolve(node.value)
            if base and re.match(r"^(flask\.)?request(\.|$)", base) and node.attr in _REQ_ATTRS:
                return "request data"
    if isinstance(node, ast.Subscript):
        n = facts.resolve(node.value) if isinstance(node.value, ast.Attribute | ast.Name) else None
        if n == "sys.argv":
            return "sys.argv"
    return None


class Taint:
    """Names tainted inside one function, with the source that tainted them."""

    def __init__(self, func: ast.FunctionDef | ast.AsyncFunctionDef, facts: FileFacts) -> None:
        self.facts = facts
        self.tainted: dict[str, str] = {}
        self.dynamic_strings: set[str] = set()
        route = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in _ROUTE_DECOS
                    or isinstance(d, ast.Attribute) and d.attr in _ROUTE_DECOS for d in func.decorator_list)
        if route:
            a = func.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
                if arg.arg not in ("self", "cls", "request", "response", "db", "session", "background_tasks"):
                    self.tainted[arg.arg] = "web route parameter"
        for _ in range(3):  # small fixed point so assignments may appear out of order inside loops
            before = len(self.tainted) + len(self.dynamic_strings)
            for node in ast.walk(func):
                self._visit(node)
            if len(self.tainted) + len(self.dynamic_strings) == before:
                break

    def source_of(self, node: ast.AST | None) -> str | None:
        """Description of the untrusted source that reaches ``node``, or None."""
        if node is None:
            return None
        s = _is_source(node, self.facts)
        if s:
            return s
        if isinstance(node, ast.Name):
            return self.tainted.get(node.id)
        if isinstance(node, ast.Call):
            name = self.facts.resolve(node.func)
            if name in _SANITIZERS or (isinstance(node.func, ast.Attribute) and node.func.attr in ("isdigit", "isalnum")):
                return None
            for a in [*node.args, *[k.value for k in node.keywords]]:
                r = self.source_of(a)
                if r:
                    return r
            if isinstance(node.func, ast.Attribute):
                return self.source_of(node.func.value)
            return None
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    r = self.source_of(v.value)
                    if r:
                        return r
            return None
        if isinstance(node, ast.BinOp):
            return self.source_of(node.left) or self.source_of(node.right)
        if isinstance(node, ast.Subscript | ast.Attribute):
            return self.source_of(node.value)
        if isinstance(node, ast.IfExp):
            return self.source_of(node.body) or self.source_of(node.orelse)
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            for e in node.elts:
                r = self.source_of(e)
                if r:
                    return r
        if isinstance(node, ast.Starred):
            return self.source_of(node.value)
        return None

    def is_dynamic_string(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
            return not (isinstance(node.left, ast.Constant) and isinstance(node.right, ast.Constant)) and (
                isinstance(node.left, ast.Constant | ast.JoinedStr) or isinstance(node.right, ast.Constant | ast.JoinedStr) or isinstance(node.op, ast.Mod))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" and isinstance(node.func.value, ast.Constant):
            return bool(node.args or node.keywords)
        if isinstance(node, ast.Name):
            return node.id in self.dynamic_strings
        return False

    def _assign(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            src = self.source_of(value)
            if src:
                self.tainted[target.id] = src
            if self.is_dynamic_string(value):
                self.dynamic_strings.add(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            src = self.source_of(value)
            if src:
                for e in target.elts:
                    if isinstance(e, ast.Name):
                        self.tainted[e.id] = src

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                self._assign(t, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self._assign(node.target, node.value)
        elif isinstance(node, ast.AugAssign):
            self._assign(node.target, ast.BinOp(left=node.target, op=node.op, right=node.value))
        elif isinstance(node, ast.For):
            src = self.source_of(node.iter)
            if src and isinstance(node.target, ast.Name):
                self.tainted[node.target.id] = src
        elif isinstance(node, ast.NamedExpr):
            self._assign(node.target, node.value)


def _calls_of(facts: FileFacts) -> list[tuple[ast.Call, Taint, str, int | None, str]]:
    """Every call inside every function with its function's taint state. Computed once per file and shared by all security-flow rules
    (previously each rule rebuilt the taint fixed point and re-walked each function)."""
    cached = facts.memo.get("sec_calls")
    if cached is None:
        out: list[tuple[ast.Call, Taint, str, int | None, str]] = []
        for fn in facts.functions:
            t = Taint(fn.node, facts)
            for node in ast.walk(fn.node):
                if isinstance(node, ast.Call):
                    out.append((node, t, fn.qualname, fn.node.lineno, facts.resolve(node.func) or ""))
        facts.memo["sec_calls"] = cached = out
    return cached  # type: ignore[return-value]


def _each_call(ctx: RuleContext) -> Iterator[tuple[ast.Call, Taint, str, int | None, str]]:
    """Calls inside functions, with the taint state of the enclosing function."""
    yield from _calls_of(ctx.facts)


def _dedupe(items: Iterator[RawFinding]) -> Iterator[RawFinding]:
    seen: set[tuple[str, int, str]] = set()
    for f in items:
        k = (f.rule_id, f.line, f.discriminator)
        if k not in seen:
            seen.add(k)
            yield f


def _attr(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else ""


# ---------------------------------------------------------------------------------------------- PY-SEC-006 SQL
_SQL_CALLS = {"execute", "executemany", "executescript", "raw", "read_sql", "read_sql_query", "read_sql_table", "exec_driver_sql"}


@rule(RuleSpec(
    id="PY-SEC-006", title="SQL statement built from dynamic text", category="security", severity="medium", languages=PY, version="1.0",
    description="A SQL statement is assembled with string formatting, concatenation or an f-string instead of query parameters, so values become part of the SQL text.",
    remediation="Pass values as bound parameters (`cursor.execute(\"... WHERE id = %s\", (value,))`, SQLAlchemy `text(...).bindparams(...)`); never format them into the statement. "
                "Identifiers that must vary should come from a fixed allow-list.",
    limitations="Only statements passed directly to execute-style calls, or through a local variable built in the same function, are recognised. Whether the inserted text is attacker "
                "controlled is only known when it visibly comes from a request, input() or argv; otherwise the finding is a construction pattern, not a confirmed injection.",
    confidence="medium", references=("CWE-89",)))
def sql_dynamic(ctx: RuleContext) -> Iterator[RawFinding]:
    out: list[RawFinding] = []
    for node, t, scope, def_line, name in _each_call(ctx):
        if _attr(node) not in _SQL_CALLS and name not in ("sqlalchemy.text", "text"):
            continue
        stmt = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ("sql", "query", "statement", "operation")), None)
        if stmt is None or not t.is_dynamic_string(stmt):
            continue
        if isinstance(stmt, ast.JoinedStr) and not any(isinstance(v, ast.FormattedValue) for v in stmt.values):
            continue
        src = t.source_of(stmt)
        line, end_line, col, end_col = loc(node)
        out.append(RawFinding("PY-SEC-006", line, end_line, col, end_col, scope, f"{_attr(node) or name}:{'tainted' if src else 'dynamic'}",
                              f"A SQL statement passed to `{_attr(node) or name}` is built with string formatting" + (f"; the inserted text comes from {src}." if src else "."),
                              f"Dynamic SQL text in `{_attr(node) or name}`" + (f" fed by {src}." if src else ", no untrusted source visible in this function."),
                              confidence="high" if src else "medium", severity="high" if src else "medium", def_line=def_line))
    yield from _dedupe(iter(out))


# ---------------------------------------------------------------------------------------------- PY-SEC-007 path
_PATH_SINKS = {"open", "builtins.open", "pathlib.Path", "pathlib.PurePath", "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.replace", "os.makedirs", "os.mkdir", "os.listdir",
               "os.scandir", "shutil.copy", "shutil.copyfile", "shutil.copy2", "shutil.move", "shutil.rmtree", "shutil.copytree", "flask.send_file", "send_file",
               "flask.send_from_directory", "send_from_directory", "fastapi.responses.FileResponse", "FileResponse", "zipfile.ZipFile", "tarfile.open"}
_CONTAIN = {"resolve", "realpath", "commonpath", "is_relative_to", "samefile"}


@rule(RuleSpec(
    id="PY-SEC-007", title="Untrusted value used as a file path", category="security", severity="high", languages=PY, version="1.0",
    description="A value from a request, input() or argv reaches a filesystem call without being reduced to a safe name, so `../` sequences or absolute paths can reach files outside the intended directory.",
    remediation="Reduce the value to a basename (`werkzeug.utils.secure_filename`, `os.path.basename`), join it to a fixed base directory, resolve the result and check that it is still inside "
                "the base (`Path.resolve().is_relative_to(base)`), or look files up by an opaque id.",
    limitations="Intraprocedural only. A containment check elsewhere in the same function lowers the finding to low confidence instead of removing it, because NOVA cannot prove the check is correct.",
    confidence="medium", references=("CWE-22",)))
def path_traversal(ctx: RuleContext) -> Iterator[RawFinding]:
    out: list[RawFinding] = []
    for fn in ctx.facts.functions:
        t = Taint(fn.node, ctx.facts)
        guarded = any(isinstance(n, ast.Call) and (_attr(n) in _CONTAIN or ctx.facts.resolve(n.func) in ("os.path.realpath", "os.path.commonpath")) for n in ast.walk(fn.node))
        for node in ast.walk(fn.node):
            if not isinstance(node, ast.Call):
                continue
            name = ctx.facts.resolve(node.func) or ""
            if name not in _PATH_SINKS:
                continue
            args = node.args[:2] if name in ("flask.send_from_directory", "send_from_directory") else node.args[:1]
            for a in args:
                src = t.source_of(a)
                if not src:
                    continue
                line, end_line, col, end_col = loc(node)
                out.append(RawFinding("PY-SEC-007", line, end_line, col, end_col, fn.qualname, f"{name}:{'guarded' if guarded else 'unguarded'}",
                                      f"A value from {src} is used as a path in `{name}`" + ("; a containment check exists in this function but NOVA cannot verify it." if guarded else " with no containment check in this function."),
                                      f"Untrusted path flows into `{name}`.", confidence="low" if guarded else "medium", severity="medium" if guarded else "high", def_line=fn.node.lineno))
                break
    yield from _dedupe(iter(out))


# ---------------------------------------------------------------------------------------------- PY-SEC-008 SSRF
_HTTP_SINKS = {f"requests.{m}" for m in ("get", "post", "put", "delete", "head", "patch", "request", "options")} | {f"httpx.{m}" for m in ("get", "post", "put", "delete", "head", "patch", "request", "stream")} | {
    "urllib.request.urlopen", "urllib.request.Request", "urllib3.request", "http.client.HTTPConnection", "http.client.HTTPSConnection"}


@rule(RuleSpec(
    id="PY-SEC-008", title="Untrusted value used as an outbound request URL", category="security", severity="high", languages=PY, version="1.0",
    description="A value from a request, input() or argv decides where the server makes an HTTP request (server-side request forgery). Attackers can target internal services or cloud metadata endpoints.",
    remediation="Accept an identifier and map it to a fixed URL, or validate the parsed host against an allow-list (and block private, loopback and link-local addresses after DNS resolution); "
                "disable redirects or revalidate each hop.",
    limitations="Intraprocedural only. If the URL begins with a fixed literal host and only the path or query is influenced, the finding is reported with low confidence.",
    confidence="medium", references=("CWE-918",)))
def ssrf(ctx: RuleContext) -> Iterator[RawFinding]:
    out: list[RawFinding] = []
    for node, t, scope, def_line, name in _each_call(ctx):
        if name not in _HTTP_SINKS:
            continue
        url = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "url"), None)
        src = t.source_of(url)
        if not src:
            continue
        fixed_host = isinstance(url, ast.JoinedStr) and url.values and isinstance(url.values[0], ast.Constant) and re.match(r"^https?://[^/{]+/", str(url.values[0].value) + "/") is not None \
            and "/" in str(url.values[0].value)[8:]
        line, end_line, col, end_col = loc(node)
        out.append(RawFinding("PY-SEC-008", line, end_line, col, end_col, scope, f"{name}:{'path-only' if fixed_host else 'url'}",
                              f"A value from {src} is used to build the URL passed to `{name}`" + ("; the host looks fixed, so only the path or query is influenced." if fixed_host else "."),
                              f"Untrusted URL flows into `{name}`.", confidence="low" if fixed_host else "medium", severity="medium" if fixed_host else "high", def_line=def_line))
    yield from _dedupe(iter(out))


# ---------------------------------------------------------------------------------------------- PY-SEC-009 weak hash
@rule(RuleSpec(
    id="PY-SEC-009", title="Weak hash algorithm (MD5 / SHA-1)", category="security", severity="low", languages=PY, version="1.0",
    description="MD5 and SHA-1 are broken for collision resistance and unsuitable for passwords, signatures or integrity against an adversary.",
    remediation="Use SHA-256 or better for integrity; use a password hashing function (argon2, bcrypt, scrypt, PBKDF2) for passwords. For non-security uses pass `usedforsecurity=False`.",
    limitations="NOVA cannot tell whether the digest protects anything. Calls inside a function or variable named like password/token/auth/signature are reported at medium severity.",
    confidence="medium", references=("CWE-327", "CWE-328")))
def weak_hash(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func) or ""
        algo = None
        if name in ("hashlib.md5", "hashlib.sha1"):
            algo = name.rsplit(".", 1)[-1]
        elif name == "hashlib.new" and node.args and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).lower() in ("md5", "sha1"):
            algo = str(node.args[0].value).lower()
        if not algo:
            continue
        if any(k.arg == "usedforsecurity" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords):
            continue
        sensitive = bool(re.search(r"pass|auth|token|sign|secret|login|credential", scope, re.IGNORECASE))
        line, end_line, col, end_col = loc(node)
        yield RawFinding("PY-SEC-009", line, end_line, col, end_col, scope, algo, f"`{algo}` is used" + (" in security-sensitive code." if sensitive else "."),
                         f"Call to hashlib {algo}.", severity="medium" if sensitive else "low", confidence="medium", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-010 logging
_SENSITIVE_NAME = re.compile(r"(?:pass(?:word|wd)?|secret|token|api[_-]?key|apikey|authorization|credential|private[_-]?key|ssn|cvv|session[_-]?id)", re.IGNORECASE)
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}


def _names_in(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
    return out


@rule(RuleSpec(
    id="PY-SEC-010", title="Sensitive value written to a log", category="security", severity="medium", languages=PY, version="1.0",
    description="A variable whose name suggests a password, token, key or credential is passed to a logging call. Log files are widely readable and long-lived.",
    remediation="Log an identifier or a redacted marker instead of the value, and remove the value from the format arguments.",
    limitations="Based on variable names only: a variable called `token_count` matches, and a credential in a variable with an innocent name is missed. The value is never displayed by NOVA.",
    confidence="medium", references=("CWE-532",)))
def sensitive_logging(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr not in _LOG_METHODS:
            continue
        base = ctx.facts.resolve(node.func.value) or ""
        if not (base.startswith("logging") or re.search(r"(^|\.)(log|logger|logging|_log|_logger|LOG|LOGGER)$", base)):
            continue
        hits = sorted({n for a in [*node.args, *[k.value for k in node.keywords]] if not (isinstance(a, ast.Constant)) for n in _names_in(a) if _SENSITIVE_NAME.search(n)})
        hits = [h for h in hits if not re.search(r"(count|len|length|size|expires|expiry|type|name|header)s?$", h, re.IGNORECASE)]
        if not hits:
            continue
        line, end_line, col, end_col = loc(node)
        yield RawFinding("PY-SEC-010", line, end_line, col, end_col, scope, f"{node.func.attr}:{','.join(hits[:3])}", f"A logging call includes `{hits[0]}`, a name that suggests a secret.",
                         f"Logging call `{base}.{node.func.attr}` references: {', '.join(hits[:3])}.", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-011 TLS
@rule(RuleSpec(
    id="PY-SEC-011", title="TLS certificate verification disabled", category="security", severity="high", languages=PY, version="1.0",
    description="Certificate or hostname verification is switched off, so a network attacker can impersonate the server and read or alter traffic.",
    remediation="Remove `verify=False` / `CERT_NONE`; trust a private CA by pointing `verify` (or the SSL context) at its certificate bundle instead.",
    limitations="Detects literal `verify=False`, `ssl._create_unverified_context`, `ssl.CERT_NONE` and `check_hostname = False`. Intentional use in tests is indistinguishable from production use.",
    confidence="high", references=("CWE-295",)))
def tls_disabled(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        what = None
        if isinstance(node, ast.Call):
            name = ctx.facts.resolve(node.func) or ""
            if name == "ssl._create_unverified_context":
                what = "ssl._create_unverified_context"
            elif any(k.arg == "verify" and isinstance(k.value, ast.Constant) and k.value.value is False for k in node.keywords):
                what = f"{name or _attr(node)}(verify=False)"
        elif isinstance(node, ast.Attribute) and ctx.facts.resolve(node) == "ssl.CERT_NONE":
            what = "ssl.CERT_NONE"
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and node.value.value is False and any(
                isinstance(t, ast.Attribute) and t.attr == "check_hostname" for t in node.targets):
            what = "check_hostname = False"
        if what:
            line, end_line, col, end_col = loc(node)
            yield RawFinding("PY-SEC-011", line, end_line, col, end_col, scope, what, f"TLS verification is disabled by `{what}`.", f"Disabled verification: {what}.", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-012 tainted command
@rule(RuleSpec(
    id="PY-SEC-012", title="Untrusted value reaches a command execution call", category="security", severity="high", languages=PY, version="1.0",
    description="A value from a request, input() or argv is part of a command run through subprocess, os.system or os.popen (command injection, or argument injection when no shell is used).",
    remediation="Never build command text from untrusted values. Use an argument list without a shell, validate against an allow-list, and pass `--` before user-supplied arguments.",
    limitations="Intraprocedural only. With an argument list and no shell the risk is argument injection and the finding is reported at medium severity.",
    confidence="medium", references=("CWE-78", "CWE-88")))
def tainted_command(ctx: RuleContext) -> Iterator[RawFinding]:
    out: list[RawFinding] = []
    sinks = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen", "os.execv", "os.execvp", "os.spawnl"}
    for node, t, scope, def_line, name in _each_call(ctx):
        if name not in sinks:
            continue
        src = next((s for s in (t.source_of(a) for a in [*node.args[:1], *[k.value for k in node.keywords if k.arg in ("args", "command", "cmd")]]) if s), None)
        if not src:
            continue
        shell = name.startswith("os.") or any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
        line, end_line, col, end_col = loc(node)
        out.append(RawFinding("PY-SEC-012", line, end_line, col, end_col, scope, f"{name}:{'shell' if shell else 'argv'}",
                              f"A value from {src} reaches `{name}`" + (" through a shell." if shell else " as an argument."), f"Untrusted value flows into `{name}`.",
                              confidence="medium" if shell else "low", severity="high" if shell else "medium", def_line=def_line))
    yield from _dedupe(iter(out))


# ---------------------------------------------------------------------------------------------- PY-SEC-013 config
@rule(RuleSpec(
    id="PY-SEC-013", title="Insecure configuration default", category="security", severity="medium", languages=PY, version="1.0",
    description="Debug mode enabled for a web application, a wildcard host list, or a wildcard CORS origin. Debug servers can expose interactive consoles and stack traces.",
    remediation="Read the setting from the environment with a safe default (`DEBUG = os.environ.get('DEBUG') == '1'`), list explicit hosts and origins.",
    limitations="Only literal `debug=True` in `.run(...)`, a module-level `DEBUG = True`, `ALLOWED_HOSTS = ['*']` and `allow_origins=['*']` are recognised.",
    confidence="medium", references=("CWE-489", "CWE-942")))
def insecure_config(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        hit = None
        if isinstance(node, ast.Call):
            if _attr(node) == "run" and any(k.arg == "debug" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                hit = ("debug-run", "A web application is started with `debug=True`.")
            elif any(k.arg in ("allow_origins", "allowed_origins") and isinstance(k.value, ast.List | ast.Tuple) and any(isinstance(e, ast.Constant) and e.value == "*" for e in k.value.elts)
                     for k in node.keywords):
                hit = ("cors-wildcard", "CORS allows every origin (`*`).")
        elif isinstance(node, ast.Assign) and not scope:
            for tg in node.targets:
                if isinstance(tg, ast.Name) and tg.id == "DEBUG" and isinstance(node.value, ast.Constant) and node.value.value is True:
                    hit = ("DEBUG", "`DEBUG = True` at module level.")
                elif isinstance(tg, ast.Name) and tg.id == "ALLOWED_HOSTS" and isinstance(node.value, ast.List) and any(isinstance(e, ast.Constant) and e.value == "*" for e in node.value.elts):
                    hit = ("ALLOWED_HOSTS", "`ALLOWED_HOSTS` contains a wildcard.")
        if hit:
            line, end_line, col, end_col = loc(node)
            yield RawFinding("PY-SEC-013", line, end_line, col, end_col, scope, hit[0], hit[1], hit[1], severity="medium" if hit[0] != "cors-wildcard" else "low", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-014 randomness
_RANDOM_FUNCS = {"random.random", "random.randint", "random.choice", "random.choices", "random.randrange", "random.getrandbits", "random.sample", "random.uniform"}


@rule(RuleSpec(
    id="PY-SEC-014", title="Non-cryptographic randomness in security-sensitive code", category="security", severity="medium", languages=PY, version="1.0",
    description="The `random` module is predictable. Tokens, passwords, nonces, salts and session identifiers must come from a cryptographically secure source.",
    remediation="Use the `secrets` module (`secrets.token_urlsafe`, `secrets.choice`) or `os.urandom`.",
    limitations="Reported only when the call is inside a function, or assigned to a variable, whose name suggests a secret (token, password, key, nonce, salt, otp, session).",
    confidence="low", references=("CWE-330", "CWE-338")))
def weak_random(ctx: RuleContext) -> Iterator[RawFinding]:
    pat = re.compile(r"token|passw|secret|nonce|salt|otp|session|api_?key|csrf|reset_?code", re.IGNORECASE)
    assigned: dict[int, str] = {}
    for node, _s, _d in ctx.facts.nodes:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Call):
                    assigned[id(sub)] = ",".join(names)
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func) or ""
        if name not in _RANDOM_FUNCS:
            continue
        ctx_name = assigned.get(id(node), "")
        if pat.search(scope) or pat.search(ctx_name):
            line, end_line, col, end_col = loc(node)
            yield RawFinding("PY-SEC-014", line, end_line, col, end_col, scope, f"{name}:{ctx_name or scope}", f"`{name}` is used where a secret value appears to be generated.",
                             f"Call to {name} in security-sensitive context.", def_line=def_line)


def analyzer() -> Analyzer:
    return Analyzer("py-security-flow", "Python security data-flow patterns", "1.0.0", PY, RULES)
