from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.services import indexer
from app.services.indexer import IndexLimits, ScannedFile, SkipCode, scan_repository

LIMITS = IndexLimits(max_files=1000, max_depth=10, max_file_bytes=10_000, max_total_bytes=1_000_000)


def scan(root: Path, limits: IndexLimits = LIMITS):
    found: list[ScannedFile] = []
    result = scan_repository(root, limits, found.append)
    return {f.rel_path: f for f in found}, result


def test_sample_repo_inventory(sample_repo: Path) -> None:
    files, result = scan(sample_repo)
    assert set(files) == {
        "README.md", "package.json", "pyproject.toml", "src/auth/login.py", "src/auth/session.py",
        "src/billing/invoice.ts", "tests/test_login.py", "docs/guide.md", ".gitignore",
        ".env.example",
    }
    assert files["src/auth/login.py"].line_count == 2
    assert len(files["README.md"].sha256) == 64
    assert result.files_indexed == len(files) and not result.truncated


def test_secrets_and_junk_never_indexed(sample_repo: Path) -> None:
    files, result = scan(sample_repo)
    for forbidden in (".env", "deploy/server.pem", "node_modules/lib/index.js",
                      "__pycache__/x.pyc", "ignored_dir/skip.py", "debug.log", "assets/logo.png",
                      "assets/data.bin"):
        assert forbidden not in files
    assert result.skipped[SkipCode.SECRET_LIKE_PATH] == 2
    assert result.skipped[SkipCode.IGNORED_PATTERN] >= 4
    assert result.skipped[SkipCode.BINARY_FILE] >= 2


def test_no_secret_content_leaks_into_results(sample_repo: Path) -> None:
    files, result = scan(sample_repo)
    blob = repr(files) + repr(result)
    assert "super-secret-value" not in blob and "PRIVATE KEY" not in blob


def test_nested_gitignore(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "pkg" / "gen").mkdir(parents=True)
    (root / "pkg" / ".gitignore").write_text("gen/\n*.tmp\n")
    (root / "pkg" / "gen" / "a.py").write_text("x")
    (root / "pkg" / "b.tmp").write_text("x")
    (root / "pkg" / "keep.py").write_text("x")
    (root / "other.tmp").write_text("x")
    files, _ = scan(root)
    assert set(files) == {"pkg/.gitignore", "pkg/keep.py", "other.tmp"}


def test_oversized_file_skipped(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "big.txt").write_text("a" * 20_000)
    (root / "small.txt").write_text("a")
    files, result = scan(root)
    assert set(files) == {"small.txt"} and result.skipped[SkipCode.FILE_TOO_LARGE] == 1


def test_max_files_truncates(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.txt").write_text("x")
    files, result = scan(root, IndexLimits(3, 10, 1000, 10_000))
    assert len(files) == 3 and result.truncated and "max_files" in (result.truncation_reason or "")


def test_total_bytes_limit(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_text("a" * 100)
    files, result = scan(root, IndexLimits(100, 10, 1000, 250))
    assert len(files) == 2 and result.truncated
    assert result.total_bytes <= 250


def test_max_depth(tmp_path: Path) -> None:
    root = tmp_path / "r"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (root / "top.txt").write_text("x")
    (root / "a" / "mid.txt").write_text("x")
    (deep / "deep.txt").write_text("x")
    files, result = scan(root, IndexLimits(100, 1, 1000, 10_000))
    assert set(files) == {"top.txt", "a/mid.txt"} and result.skipped[SkipCode.MAX_DEPTH_EXCEEDED] == 1


def test_symlinks_are_not_followed(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1")
    (root / "real.py").write_text("x = 1")
    try:
        os.symlink(outside, root / "linkdir", target_is_directory=True)
        os.symlink(outside / "secret.py", root / "linkfile.py")
    except OSError:
        pytest.skip("symlinks unavailable")
    files, result = scan(root)
    assert set(files) == {"real.py"} and result.skipped[SkipCode.SYMLINK_REJECTED] == 2


def test_unreadable_file_does_not_abort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.txt").write_text("x")
    (root / "b.txt").write_text("x")
    real = indexer._read_file

    def flaky(path: Path, max_bytes: int):
        if path.name == "a.txt":
            raise PermissionError("denied")
        return real(path, max_bytes)

    monkeypatch.setattr(indexer, "_read_file", flaky)
    files, result = scan(root)
    assert set(files) == {"b.txt"} and result.skipped[SkipCode.UNREADABLE] == 1


def test_directory_escaping_root_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defence in depth: a directory whose canonical location is outside the root is not entered."""
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "sub" / "x.py").write_text("x")
    real_is_within = indexer.is_within

    def fake(path: Path, base: Path) -> bool:
        return False if path.name == "sub" else real_is_within(path, base)

    monkeypatch.setattr(indexer, "is_within", fake)
    files, result = scan(root)
    assert files == {} and result.skipped[SkipCode.OUTSIDE_REPOSITORY] == 1


def test_empty_directory(tmp_path: Path) -> None:
    files, result = scan(tmp_path)
    assert files == {} and result.files_indexed == 0


def test_invalid_encoding_is_skipped_with_reason(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"x = '\xff\xfe broken'\n")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    files, result = scan(tmp_path)
    assert set(files) == {"ok.py"} and result.skipped[SkipCode.UNSUPPORTED_ENCODING] == 1


def test_utf8_bom_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
    files, _ = scan(tmp_path)
    assert "bom.py" in files


def test_unicode_and_hidden_file_names(tmp_path: Path) -> None:
    (tmp_path / "módulo_ü.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("y = 2\n")
    (tmp_path / "with space.py").write_text("z = 3\n")
    files, _ = scan(tmp_path)
    assert {"módulo_ü.py", ".hidden.py", "with space.py"} <= set(files)


def test_secret_like_paths_are_counted_but_never_named(tmp_path: Path) -> None:
    (tmp_path / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----")
    (tmp_path / "ok.py").write_text("x = 1\n")
    files, result = scan(tmp_path)
    assert set(files) == {"ok.py"}
    secret = [d for d in result.diagnostics if d.code == SkipCode.SECRET_LIKE_PATH]
    assert len(secret) == 1 and secret[0].rel_path is None


def test_diagnostics_are_bounded(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"b{i}.png").write_bytes(b"\x89PNG\x00\x00")
    limits = IndexLimits(1000, 10, 10_000, 1_000_000, max_diagnostics=5)
    _, result = scan(tmp_path, limits)
    assert len(result.diagnostics) <= 5 and result.skipped[SkipCode.BINARY_FILE] == 30
