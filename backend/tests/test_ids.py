"""Row ids are time-ordered, so ``order_by(created_at, id)`` keeps creation order when timestamps tie."""

from __future__ import annotations

import re
import threading

from app.db.base import new_id


def test_ids_are_32_hex_and_unique() -> None:
    ids = [new_id() for _ in range(5000)]
    assert all(re.fullmatch(r"[0-9a-f]{32}", i) for i in ids)
    assert len(set(ids)) == len(ids)


def test_ids_strictly_increase_even_within_one_clock_tick() -> None:
    ids = [new_id() for _ in range(5000)]  # far faster than the clock resolution on Windows
    assert ids == sorted(ids)
    assert len(set(i[:16] for i in ids)) == len(ids), "the timestamp prefix alone must be unique"


def test_ids_stay_unique_across_threads() -> None:
    out: list[str] = []
    lock = threading.Lock()

    def work() -> None:
        mine = [new_id() for _ in range(1000)]
        with lock:
            out.extend(mine)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == len(out) == 8000
