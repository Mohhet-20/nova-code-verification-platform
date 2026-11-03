"""Per-client throttling of failed bearer-token authentication.

Only a SUPPLIED but invalid token counts as a failure (a missing token cannot be used to guess one). After ``limit`` failures inside the sliding
window the client gets 429 until the oldest failure leaves the window, even with a valid token. State is in memory and per process: it is a brake on
online guessing against one API process, not a distributed rate limiter. The client key is the connection peer as resolved by the ASGI server, so
behind a reverse proxy the server must trust the proxy's forwarding headers (uvicorn ``--forwarded-allow-ips`` / ``FORWARDED_ALLOW_IPS``).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

MAX_TRACKED_CLIENTS = 10_000


class AuthFailureThrottle:
    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic) -> None:  # type: ignore[no-untyped-def]
        self.limit, self.window, self._clock = limit, float(window_seconds), clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _prune(self, key: str, now: float) -> deque[float] | None:
        q = self._failures.get(key)
        if q is None:
            return None
        while q and q[0] <= now - self.window:
            q.popleft()
        if not q:
            del self._failures[key]
            return None
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until the client may try again, or 0 if it is not blocked."""
        if not self.enabled:
            return 0
        with self._lock:
            now = self._clock()
            q = self._prune(key, now)
            if q is None or len(q) < self.limit:
                return 0
            return max(1, int(q[0] + self.window - now) + 1)

    def record_failure(self, key: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = self._clock()
            q = self._prune(key, now)
            if q is None:
                q = self._failures[key] = deque(maxlen=self.limit)
                while len(self._failures) > MAX_TRACKED_CLIENTS:  # bounded memory under a spray of source addresses
                    self._failures.popitem(last=False)
            q.append(now)
            self._failures.move_to_end(key)
