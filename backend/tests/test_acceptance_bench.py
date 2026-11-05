"""Synthetic benchmarks for governance, review-queue and webhook paths. They run as ordinary tests with deliberately generous ceilings
(so they are not flaky on a 2-CPU sandbox) and write their measurements to $NOVA_BENCH_OUT when set. Timings are from the environment that ran them."""
from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Callable
from typing import Any

from tests.test_governance import World, w  # noqa: F401
from tests.test_integrations import KEY, check_run_body, deliver_gh, env, gh_install  # noqa: F401

RESULTS: dict[str, Any] = {}


def measure(name: str, fn: Callable[[], Any], n: int = 15, check: Callable[[Any], bool] | None = None) -> dict[str, float]:
    fn()  # warm-up, excluded
    samples, errors = [], 0
    for _ in range(n):
        t = time.perf_counter()
        r = fn()
        samples.append((time.perf_counter() - t) * 1000)
        if (not check(r)) if check else getattr(r, "status_code", 200) >= 400:
            errors += 1
    samples.sort()
    out = {"n": n, "median_ms": round(statistics.median(samples), 2), "min_ms": round(samples[0], 2), "max_ms": round(samples[-1], 2),
           "p95_ms": round(samples[int(0.95 * (n - 1))], 2) if n >= 20 else None, "errors": errors}
    RESULTS[name] = out
    if path := os.environ.get("NOVA_BENCH_OUT"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(RESULTS, f, indent=2)
    return out


def test_governance_and_queue_benchmarks(w: World) -> None:  # noqa: F811
    n_items = 120
    for i in range(n_items):
        rev = w.review(w.ids["repoA"], author="eng2", head=f"sha{i}")
        w.submit("eng1", w.instance("eng1", "code_review", rev, title=f"Item {i}")["id"])
    ceilings = {}
    ceilings["review_queue_page"] = measure("review_queue_page_50", lambda: w.get("/review-queue?page_size=50", "rev1"), check=lambda r: r.json()["total"] >= n_items)
    ceilings["workflow_list"] = measure("workflow_instances_list", lambda: w.get("/workflows/instances?page_size=50", "eng1"))
    ceilings["overview"] = measure("governance_overview", lambda: w.get("/governance/overview", "lead"))
    ceilings["audit_page"] = measure("audit_page_100", lambda: w.get("/governance/audit?page_size=100", "aud"))
    rev = w.review(w.ids["repoA"], author="eng2", head="evalsha")
    ceilings["evaluation"] = measure("policy_evaluation", lambda: w.post("/governance/evaluations", "lead", {"resource_type": "code_review", "resource_id": rev}))
    for name, m in ceilings.items():
        assert m["errors"] == 0, (name, m)
        assert m["median_ms"] < 2000, (name, m)  # generous ceiling for a shared 2-CPU sandbox; real numbers are in the report


def test_webhook_processing_benchmark_and_dedupe(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    inst = gh_install(env)
    assert env.call("POST", f"/integrations/installations/{inst['id']}/enable").status_code == 200
    counter = iter(range(10_000))

    def deliver_new():  # type: ignore[no-untyped-def]
        i = next(counter)
        return deliver_gh(env, inst, check_run_body(run_id=1000 + i), delivery=f"bench-delivery-{i:06d}")

    probe = deliver_gh(env, inst, check_run_body(run_id=5), delivery="probe-delivery-0001")
    assert probe.status_code in (200, 201, 202), probe.text
    m = measure("webhook_inbound_signed_new", deliver_new, n=40, check=lambda r: r.status_code in (200, 201, 202))
    assert m["errors"] == 0 and m["median_ms"] < 2000, m
    body = check_run_body(run_id=77)
    first = deliver_gh(env, inst, body, delivery="dup-delivery-0001")
    d = measure("webhook_inbound_duplicate", lambda: deliver_gh(env, inst, body, delivery="dup-delivery-0001"), n=20, check=lambda r: r.status_code == first.status_code)
    assert d["errors"] == 0, d
    bad = measure("webhook_inbound_bad_signature", lambda: deliver_gh(env, inst, body, delivery="bad-delivery-0001", sig="sha256=" + "0" * 64), n=20, check=lambda r: r.status_code in (401, 403))
    assert bad["errors"] == 0, bad
