from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.errors import InvalidRepositoryError, UnsafePathError
from app.security.paths import resolve_repository_root, safe_join
from app.security.secrets import is_secret_filename


def test_resolve_valid_root_is_canonical(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    messy = f"{tmp_path}/r/../r"
    assert resolve_repository_root(messy) == (tmp_path / "r").resolve()


@pytest.mark.parametrize("raw", ["", "   ", "relative/path"])
def test_rejects_empty_or_relative(raw: str) -> None:
    with pytest.raises(InvalidRepositoryError):
        resolve_repository_root(raw)


def test_rejects_missing_file_and_nul(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(InvalidRepositoryError):
        resolve_repository_root(str(tmp_path / "missing"))
    with pytest.raises(InvalidRepositoryError):
        resolve_repository_root(str(f))
    with pytest.raises(UnsafePathError):
        resolve_repository_root(str(tmp_path) + "\x00evil")


def test_rejects_filesystem_root_and_home() -> None:
    with pytest.raises(InvalidRepositoryError):
        resolve_repository_root(str(Path(Path.cwd().anchor)))
    with pytest.raises(InvalidRepositoryError):
        resolve_repository_root(str(Path.home()))


def test_allowed_base_directories(tmp_path: Path) -> None:
    allowed, other = tmp_path / "allowed", tmp_path / "other"
    (allowed / "repo").mkdir(parents=True)
    other.mkdir()
    assert resolve_repository_root(str(allowed / "repo"), [str(allowed)])
    with pytest.raises(UnsafePathError):
        resolve_repository_root(str(other), [str(allowed)])


def test_symlinked_root_resolves_to_target_for_base_check(tmp_path: Path) -> None:
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        resolve_repository_root(str(link), [str(allowed)])


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "..\\x", "", "a\x00b"])
def test_safe_join_rejects_traversal(tmp_path: Path, bad: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, bad)


def test_safe_join_accepts_nested_path(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    assert safe_join(tmp_path, "a/b.txt") == tmp_path.resolve() / "a" / "b.txt"


def test_safe_join_rejects_symlink_escape(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(UnsafePathError):
        safe_join(root, "link/secret.txt")


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", "server.pem", "id_rsa",
                                  "id_ed25519.pub", "credentials.json", "secrets.yaml", ".npmrc",
                                  "terraform.tfstate", "prod.tfvars", "service-account-prod.json",
                                  "KEY.PEM"])
def test_secret_names_detected(name: str) -> None:
    assert is_secret_filename(name)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", "main.py", "README.md",
                                  "environment.ts", "keyboard.ts"])
def test_ordinary_names_not_flagged(name: str) -> None:
    assert not is_secret_filename(name)
