"""Security-oriented *patterns* for Python. These are suspicious constructs, never confirmed vulnerabilities.

Findings never contain the suspected secret: evidence only states the kind of match, the variable name and the length.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator

from app.services.analysis.analyzers.py_facts import loc
from app.services.analysis.model import Analyzer, RawFinding, Rule, RuleContext, RuleSpec
from app.services.analysis.redaction import TOKEN_PATTERNS

PY = ("Python",)
RULES: list[Rule] = []


def rule(spec: RuleSpec):  # type: ignore[no-untyped-def]
    def deco(fn):  # type: ignore[no-untyped-def]
        RULES.append(Rule(spec, fn))
        return fn

    return deco


def _is_constant_text(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


# ---------------------------------------------------------------------------------------------- PY-SEC-001
@rule(RuleSpec(
    id="PY-SEC-001", title="Dynamic code execution (eval/exec)", category="security", severity="medium", languages=PY,
    version="1.0",
    description="`eval` and `exec` run text as code. That is dangerous if any part of the text can be influenced by an attacker.",
    remediation="Prefer `ast.literal_eval`, a dispatch table, or a parser for the specific format. If dynamic execution is "
                "required, ensure the input is fully trusted and document why.",
    limitations="NOVA cannot tell where the argument comes from. A constant string argument is reported at low severity; "
                "anything else is flagged because its origin is unknown, not because it is known to be attacker-controlled.",
    confidence="medium", references=("CWE-95",)))
def dynamic_exec(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func)
        if name not in ("eval", "exec", "builtins.eval", "builtins.exec"):
            continue
        fn = name.rsplit(".", 1)[-1]
        const = bool(node.args) and _is_constant_text(node.args[0])
        line, end_line, col, end_col = loc(node)
        yield RawFinding("PY-SEC-001", line, end_line, col, end_col, scope, f"{fn}:{'const' if const else 'dynamic'}",
                         f"`{fn}()` is called with {'a constant string' if const else 'a non-constant argument'}.",
                         f"Call to builtin {fn}; argument is {'a literal' if const else 'not a literal'}.",
                         severity="low" if const else None, def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-002
_SUBPROCESS = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen"}
_OS_SHELL = {"os.system", "os.popen"}


def _dynamic_command(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_dynamic_command(e) for e in node.elts)
    return True


@rule(RuleSpec(
    id="PY-SEC-002", title="Shell command execution", category="security", severity="medium", languages=PY, version="1.0",
    description="Running a command through a shell (`shell=True`, `os.system`, `os.popen`) allows shell metacharacters in "
                "the command text to change its meaning.",
    remediation="Pass an argument list to `subprocess.run([...])` without `shell=True`; use `shlex.quote` if a shell is unavoidable.",
    limitations="Whether the command contains attacker-influenced text cannot be determined statically. A non-constant "
                "command is reported as higher severity; a constant command is reported as low.",
    confidence="medium", references=("CWE-78",)))
def shell_command(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func)
        if name is None:
            continue
        shell = name in _OS_SHELL
        if name in _SUBPROCESS:
            shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                        for k in node.keywords)
        if not shell:
            continue
        cmd = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ("args", "command", "cmd")), None)
        dynamic = _dynamic_command(cmd)
        line, end_line, col, end_col = loc(node)
        yield RawFinding("PY-SEC-002", line, end_line, col, end_col, scope, f"{name}:{'dyn' if dynamic else 'const'}",
                         f"`{name}` runs a {'non-constant' if dynamic else 'constant'} command through a shell.",
                         f"Shell execution via {name}; command argument is {'built at runtime' if dynamic else 'a literal'}.",
                         severity="high" if dynamic else "low", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-003
_CRED_NAME = re.compile(r"(?:^|_)(?:password|passwd|pwd|passphrase|secret|api_?key|access_?key|private_?key|"
                        r"auth_?token|token|credentials?|client_?secret)s?$")
_PLACEHOLDER = re.compile(r"^(?:[<{$%].*|\*+|x+|-+|\.+|none|null|todo|changeme|change_me|password|secret|token|example|"
                          r"dummy|test|redacted|your[_-].*|env:.*)$", re.IGNORECASE)


def _name_of(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript) and _is_constant_text(target.slice):
        return str(target.slice.value)  # type: ignore[attr-defined]
    return None


def _credential_like(name: str, value: str) -> bool:
    if not _CRED_NAME.search(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()):
        return False
    return len(value) >= 8 and not any(c.isspace() for c in value) and not _PLACEHOLDER.match(value)


@rule(RuleSpec(
    id="PY-SEC-003", title="Hardcoded credential-like value", category="security", severity="medium", languages=PY,
    version="1.0",
    description="A string literal that looks like a password, key or token is assigned in source code, or a value has the "
                "exact shape of a well-known credential.",
    remediation="Load secrets from the environment or a secrets manager, rotate any value that was committed, and keep "
                "examples as obvious placeholders.",
    limitations="Heuristic: a variable named like a credential with a non-placeholder literal, or a known token shape. "
                "Test fixtures and sample values are indistinguishable from real ones. The value is never shown.",
    confidence="medium", support="partial", references=("CWE-798",)))
def hardcoded_credential(ctx: RuleContext) -> Iterator[RawFinding]:
    # Literals with a well-known credential shape are reported once, by shape (higher confidence), not also by name.
    shaped: dict[int, str] = {}
    for node, _scope, _dl in ctx.facts.nodes:
        if _is_constant_text(node) and len(node.value) < 5000:  # type: ignore[attr-defined]
            for label, pattern in TOKEN_PATTERNS.items():
                if pattern.search(node.value):  # type: ignore[attr-defined]
                    shaped[id(node)] = label
                    break

    def finding(node: ast.AST, scope: str, def_line: int | None, name: str, value: str) -> RawFinding:
        line, end_line, col, end_col = loc(node)
        return RawFinding(
            "PY-SEC-003", line, end_line, col, end_col, scope, f"name:{name.lower()}",
            f"`{name}` is assigned a string literal that looks like a credential.",
            f"String literal of {len(value)} characters assigned to `{name}`. The value is not stored or displayed.",
            confidence="medium" if len(value) >= 12 and any(c.isdigit() for c in value) else "low", def_line=def_line)

    for node, scope, def_line in ctx.facts.nodes:
        pairs: list[tuple[ast.AST, str, str]] = []  # (anchor node, name, value)
        if isinstance(node, ast.Assign) and _is_constant_text(node.value):
            pairs = [(node, n, node.value.value) for t in node.targets if (n := _name_of(t))]  # type: ignore[attr-defined]
        elif isinstance(node, ast.AnnAssign) and node.value is not None and _is_constant_text(node.value):
            if (n := _name_of(node.target)):
                pairs = [(node, n, node.value.value)]  # type: ignore[attr-defined]
        elif isinstance(node, ast.keyword) and node.arg and _is_constant_text(node.value):
            pairs = [(node.value, node.arg, node.value.value)]  # type: ignore[attr-defined]
        elif isinstance(node, ast.Dict):
            pairs = [(v, k.value, v.value) for k, v in zip(node.keys, node.values, strict=True)  # type: ignore[attr-defined]
                     if k is not None and _is_constant_text(k) and _is_constant_text(v)]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            pos = [*a.posonlyargs, *a.args]
            for arg, d in list(zip(pos[len(pos) - len(a.defaults):], a.defaults, strict=False)) + [
                    (x, y) for x, y in zip(a.kwonlyargs, a.kw_defaults, strict=True) if y is not None]:
                if _is_constant_text(d):
                    pairs.append((d, arg.arg, d.value))  # type: ignore[attr-defined]
        for anchor, name, value in pairs:
            vnode = anchor.value if isinstance(anchor, (ast.Assign, ast.AnnAssign)) else anchor
            if id(vnode) in shaped:
                continue
            if _credential_like(name, value):
                yield finding(anchor, scope, def_line, name, value)
    for node, scope, def_line in ctx.facts.nodes:
        label = shaped.get(id(node))
        if label:
            line, end_line, col, end_col = loc(node)
            yield RawFinding(
                "PY-SEC-003", line, end_line, col, end_col, scope, f"shape:{label}",
                f"A string literal has the shape of a {label}.",
                f"Pattern match: {label}. The value is not stored or displayed.", confidence="high",
                severity="high", def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-004
@rule(RuleSpec(
    id="PY-SEC-004", title="Insecure temporary file pattern", category="security", severity="medium", languages=PY,
    version="1.0",
    description="`tempfile.mktemp` returns a name without creating the file, which allows another process to create it first. "
                "Opening a fixed path under /tmp is similarly predictable.",
    remediation="Use `tempfile.NamedTemporaryFile`, `tempfile.mkstemp` or `tempfile.TemporaryDirectory`.",
    limitations="Only `tempfile.mktemp` and `open()` with a literal /tmp or /var/tmp path are recognised.",
    confidence="high", references=("CWE-377",)))
def insecure_tempfile(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func)
        line, end_line, col, end_col = loc(node)
        if name == "tempfile.mktemp":
            yield RawFinding("PY-SEC-004", line, end_line, col, end_col, scope, "mktemp",
                             "`tempfile.mktemp()` creates a predictable, race-prone file name.",
                             "Call to tempfile.mktemp.", def_line=def_line)
        elif name in ("open", "builtins.open") and node.args and _is_constant_text(node.args[0]) and \
                node.args[0].value.startswith(("/tmp/", "/var/tmp/")):  # type: ignore[attr-defined]  # noqa: S108
            yield RawFinding("PY-SEC-004", line, end_line, col, end_col, scope, "fixed-tmp-path",
                             "A file under a fixed /tmp path is opened.",
                             "open() with a literal /tmp or /var/tmp path.", severity="low", confidence="medium",
                             def_line=def_line)


# ---------------------------------------------------------------------------------------------- PY-SEC-005
_UNSAFE_LOADERS = {"pickle.load", "pickle.loads", "cPickle.load", "cPickle.loads", "dill.load", "dill.loads",
                   "marshal.load", "marshal.loads", "shelve.open", "jsonpickle.decode", "yaml.unsafe_load"}
_SAFE_YAML = {"SafeLoader", "CSafeLoader", "BaseLoader"}


@rule(RuleSpec(
    id="PY-SEC-005", title="Potentially unsafe deserialization", category="security", severity="medium", languages=PY,
    version="1.0",
    description="Unpickling, `marshal`, `shelve` and non-safe YAML loading can execute code embedded in the data.",
    remediation="Use JSON or another data-only format for untrusted input, `yaml.safe_load`, or authenticate the data "
                "(for example with an HMAC) before deserializing.",
    limitations="Only dangerous if the data can come from an untrusted party, which cannot be determined statically. "
                "Reading files that the application itself wrote is often acceptable.",
    confidence="medium", references=("CWE-502",)))
def unsafe_deserialization(ctx: RuleContext) -> Iterator[RawFinding]:
    for node, scope, def_line in ctx.facts.nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ctx.facts.resolve(node.func)
        if name is None:
            continue
        unsafe = name in _UNSAFE_LOADERS
        if name == "yaml.load":
            loader = next((k.value for k in node.keywords if k.arg == "Loader"), node.args[1] if len(node.args) > 1 else None)
            loader_name = ctx.facts.resolve(loader) if loader is not None else None
            unsafe = loader_name is None or loader_name.rsplit(".", 1)[-1] not in _SAFE_YAML
        if unsafe:
            line, end_line, col, end_col = loc(node)
            yield RawFinding("PY-SEC-005", line, end_line, col, end_col, scope, name,
                             f"`{name}` can execute code contained in the data it loads.",
                             f"Call to {name}" + (" without a safe loader." if name == "yaml.load" else "."),
                             def_line=def_line)


def analyzer() -> Analyzer:
    return Analyzer("py-security", "Python security patterns", "1.0.0", PY, RULES)
