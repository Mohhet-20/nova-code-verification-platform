"""Measure indexing performance on a generated repository. Run from backend/ with the venv active:

    python ../scripts/measure_indexing.py --files 3000

Reports wall-clock timings for a first index, an unchanged re-index, a one-file-change re-index, a
freshness check and symbol searches, plus the environment. Numbers are only meaningful together with
that environment; they are not a guarantee on other hardware.
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import tempfile
import time
from pathlib import Path


def generate(root: Path, n: int) -> None:
    for i in range(n):
        pkg = root / f"pkg{i % 40}" / f"sub{i % 7}"
        pkg.mkdir(parents=True, exist_ok=True)
        if i % 3 == 0:
            body = "\n".join(
                [f"import os\nfrom pkg{(i + 1) % 40}.sub{(i + 1) % 7} import mod{i + 1}\n"]
                + [f"class Thing{i}_{k}:\n    def method_{k}(self, a, b):\n        return a + b\n" for k in range(6)]
                + [f"def helper_{i}_{k}(x):\n    return x * {k}\n" for k in range(6)])
            (pkg / f"mod{i}.py").write_text(body)
        elif i % 3 == 1:
            body = f'import {{ dep }} from "./mod{i - 1}";\n' + "\n".join(
                f"export class Widget{i}_{k} {{ render() {{ return {k}; }} }}\nexport const fn{i}_{k} = (a) => a + {k};"
                for k in range(6))
            (pkg / f"mod{i}.ts").write_text(body)
        else:
            (pkg / f"notes{i}.md").write_text(f"# notes {i}\n" * 20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=3000)
    n = ap.parse_args().files
    tmp = Path(tempfile.mkdtemp(prefix="nova-perf-"))
    os.environ["NOVA_DATABASE_URL"] = f"sqlite:///{(tmp / 'perf.db').as_posix()}"
    os.environ["NOVA_INDEX_MAX_FILES"] = str(max(n * 2, 1000))
    os.environ["NOVA_LOG_LEVEL"] = "WARNING"
    from alembic import command
    from alembic.config import Config
    from fastapi.testclient import TestClient

    backend = Path.cwd()
    cfg = Config(str(backend / "alembic.ini"))
    command.upgrade(cfg, "head")
    from app.main import create_app

    repo_root = tmp / "repo"
    t0 = time.perf_counter()
    generate(repo_root, n)
    gen = time.perf_counter() - t0
    api = "/api/v1"
    results: dict[str, object] = {}
    with TestClient(create_app()) as c:
        rid = c.post(f"{api}/repositories", json={"name": "perf", "root_path": str(repo_root)}).json()["id"]

        def run(label: str, mode: str = "incremental") -> dict:
            t = time.perf_counter()
            r = c.post(f"{api}/repositories/{rid}/index", json={"mode": mode})
            snap = c.get(f"{api}/repositories/{rid}/snapshots/{r.json()['id']}").json()
            results[label] = round(time.perf_counter() - t, 3)
            return snap

        first = run("first_index_s")
        run("unchanged_reindex_s")
        (repo_root / "pkg0" / "sub0" / "mod0.py").write_text("def changed():\n    return 1\n")
        changed = run("one_file_changed_reindex_s")
        run("full_reindex_s", "full")
        t = time.perf_counter()
        fresh = c.get(f"{api}/repositories/{rid}/freshness").json()
        results["freshness_s"] = round(time.perf_counter() - t, 3)
        times = []
        for q in ("thing", "helper_5", "widget12", "nomatchzzz"):
            t = time.perf_counter()
            c.get(f"{api}/repositories/{rid}/symbols/search", params={"q": q})
            times.append(time.perf_counter() - t)
        results["symbol_search_ms_max"] = round(max(times) * 1000, 1)
        results["symbol_search_ms_avg"] = round(sum(times) / len(times) * 1000, 1)
        # Phase 03 graph endpoints (each request is a fresh traversal; nothing is cached).
        def timed(label: str, url: str, **params: object) -> None:
            t = time.perf_counter()
            r = c.get(f"{api}/repositories/{rid}{url}", params=params)
            results[label] = f"{round((time.perf_counter() - t) * 1000, 1)} ms (HTTP {r.status_code})"

        timed("metrics", "/metrics")
        timed("cycles", "/dependencies/cycles")
        timed("dependencies_depth5", "/dependencies", path="pkg1/sub1/mod1.ts", depth=5)
        timed("dependents_depth5", "/dependencies", path="pkg1/sub1/mod1.ts", direction="in", depth=5)
        timed("impact_depth10", "/dependencies/impact", path="pkg1/sub1/mod1.ts", max_depth=10)
        sym = c.get(f"{api}/repositories/{rid}/symbols", params={"q": "helper_3_1"}).json()["items"]
        if sym:
            timed("symbol_detail", f"/symbols/{sym[0]['stable_id']}")
            timed("symbol_impact", "/dependencies/impact", symbol_id=sym[0]["stable_id"], max_depth=10)
    print("environment:", platform.platform(), "| python", sys.version.split()[0], "| cpus", os.cpu_count())
    print(f"fixture: {n} generated files ({gen:.1f}s to generate)")
    print(f"first index: files={first['files_indexed']} symbols={first['symbols_count']} "
          f"imports={first['imports_total']} relationships={first['relationships_count']} "
          f"server_duration_ms={first['duration_ms']}")
    print(f"changed run: new={changed['files_new']} changed={changed['files_changed']} "
          f"unchanged={changed['files_unchanged']}")
    print("freshness:", fresh["state"])
    for k, v in results.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
