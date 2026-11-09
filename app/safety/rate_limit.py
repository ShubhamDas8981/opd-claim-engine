"""Sliding-window rate limiter. In memory = correct for ONE API process.
Running several workers/containers? Swap the dict for Redis (INCR + EXPIRE) behind the same `hit()` function."""
import threading
import time
from collections import defaultdict, deque

from app.safety import config

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_last_sweep = 0.0


def hit(bucket: str, key: str) -> bool:
    """Records a call. True = allowed, False = over the limit."""
    limit, window = config.RATE_LIMITS[bucket]
    now = time.monotonic()
    global _last_sweep
    with _lock:
        q = _hits[(bucket, key)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if now - _last_sweep > 300:                       # drop idle keys so memory stays small
            _last_sweep = now
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > config.RATE_LIMITS[k[0]][1]]:
                _hits.pop(k, None)
        return True


def reset():                                              # for tests
    with _lock:
        _hits.clear()
