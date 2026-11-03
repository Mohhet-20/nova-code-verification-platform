from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import BACKEND_DIR, reset_settings_cache
from app.db.session import reset_engine

if os.name == "nt":
    # Fixture repositories are written with Path.write_text, which on Windows turns "\n" into "\r\n". The patch/refactor engines support LF
    # files only (an explicit `unsupported_line_endings` blocker, documented), so every fixture would be refused. Only calls made FROM TEST
    # MODULES are forced to LF; application code keeps the platform default, so a genuine newline bug in the app still shows up.
    _original_write_text = Path.write_text

    def _lf_write_text(self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None) -> int:
        if newline is None and sys._getframe(1).f_globals.get("__name__", "").startswith("tests."):
            newline = "\n"
        return _original_write_text(self, data, encoding, errors, newline)

    Path.write_text = _lf_write_text  # type: ignore[method-assign]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{(tmp_path / 'nova-test.db').as_posix()}"
    monkeypatch.setenv("NOVA_DATABASE_URL", url)
    for var in ("NOVA_API_TOKEN", "NOVA_REGISTRATION_BASE_DIRS", "NOVA_AI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NOVA_LOG_JSON", "false")
    reset_settings_cache()
    reset_engine()
    return url


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The schema is migrated to head once per session (migrations are still exercised end to end here and in test_health_and_migrations);
    each test then gets its own byte copy, which is equivalent to a fresh upgraded database and ~5 s faster."""
    path = tmp_path_factory.mktemp("template") / "template.db"
    command.upgrade(alembic_config(f"sqlite:///{path.as_posix()}"), "head")
    return path


@pytest.fixture
def migrated_url(db_url: str, _migrated_template: Path) -> str:
    import shutil

    shutil.copyfile(_migrated_template, db_url.removeprefix("sqlite:///"))
    return db_url


@pytest.fixture
def client(migrated_url: str) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_engine()
    reset_settings_cache()


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A synthetic repository that exercises ignore rules, secrets and binaries."""
    root = tmp_path / "sample-repo"
    files = {
        "README.md": "# Sample\n",
        "package.json": '{"name": "sample"}\n',
        "pyproject.toml": "[project]\nname='sample'\n",
        "src/auth/login.py": "def login(user, password):\n    return True\n",
        "src/auth/session.py": "SESSION = {}\n",
        "src/billing/invoice.ts": "export const invoice = 1;\n",
        "tests/test_login.py": "def test_login():\n    assert True\n",
        "docs/guide.md": "guide\n",
        ".gitignore": "ignored_dir/\n*.log\n",
        "ignored_dir/skip.py": "x = 1\n",
        "debug.log": "log\n",
        ".env": "API_KEY=super-secret-value\n",
        ".env.example": "API_KEY=\n",
        "deploy/server.pem": "-----BEGIN PRIVATE KEY-----\n",
        "node_modules/lib/index.js": "module.exports = 1;\n",
        "__pycache__/x.pyc": "bin",
        "assets/logo.png": "\x89PNG\x00\x00",
        "assets/data.bin": "\x00\x01\x02",
    }
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("latin-1"))
    return root


@pytest.fixture
def git_repo(sample_repo: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(sample_repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@example.com", "-c", "user.name=T", "add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
    return sample_repo


def register_and_index(client: TestClient, root: Path, name: str = "sample") -> dict:
    created = client.post("/api/v1/repositories", json={"name": name, "root_path": str(root)})
    assert created.status_code == 201, created.text
    repo = created.json()
    started = client.post(f"/api/v1/repositories/{repo['id']}/index")
    assert started.status_code == 202, started.text
    snap = client.get(f"/api/v1/repositories/{repo['id']}/snapshots/{started.json()['id']}").json()
    return {"repo": repo, "snapshot": snap}
