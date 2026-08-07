# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""In-process stand-in for the Redis client.

The ERP deploy runs no Redis, so `redis_instance()` no longer returns a
`redis.Redis`; it returns a process-local store implementing the subset of the
client that the call sites actually use. Callers are unchanged.

Scope of the emulation — every use in the codebase outside tests:
  * `issue_activities_task`  set(ex=)/get           request origin, 10 min
  * `email_notification_task` set(nx=, ex=)/get/delete   send-once lock
  * `magic_code`             set(ex=)/get/exists/delete/ttl/eval  login codes

Semantics follow redis-py so the call sites keep working verbatim: `get`
returns bytes or None, `set(nx=True)` returns None when the key is taken,
`ttl` returns -2/-1 like Redis, `exists`/`delete` return counts.

Consequences of being in-process, accepted deliberately with LocMemCache:
each gunicorn worker holds its own store and it is lost on restart. For the
origin hint and the send-once lock that only costs a duplicate at worst; magic
-link login would break across workers, and the ERP deploy does not use it
(auth is the API token, no web UI is served).
"""

import threading
import time


class InProcessStore:
    """Thread-safe dict with Redis-style expiry. Not shared between processes."""

    # Guard against unbounded growth: every key here is written with an expiry,
    # so a sweep on write is enough to keep the store bounded in practice.
    _SWEEP_EVERY = 256

    def __init__(self):
        self._data = {}  # key -> (value: bytes, expires_at: float | None)
        self._lock = threading.Lock()
        self._writes = 0

    # ---------- internals ----------

    @staticmethod
    def _key(key):
        return key.decode() if isinstance(key, bytes) else str(key)

    @staticmethod
    def _value(value):
        if isinstance(value, bytes):
            return value
        return str(value).encode()

    def _live(self, key, now):
        """Entry for `key` if present and unexpired, else None. Call under lock."""
        entry = self._data.get(key)
        if entry is None:
            return None
        _, expires_at = entry
        if expires_at is not None and expires_at <= now:
            del self._data[key]
            return None
        return entry

    def _sweep(self, now):
        """Drop expired keys. Call under lock."""
        for key in [k for k, (_, exp) in self._data.items() if exp is not None and exp <= now]:
            del self._data[key]

    # ---------- redis-py surface ----------

    def get(self, key):
        key = self._key(key)
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            return None if entry is None else entry[0]

    def set(self, key, value, nx=False, ex=None):
        key = self._key(key)
        now = time.monotonic()
        with self._lock:
            if nx and self._live(key, now) is not None:
                return None  # redis-py returns None when NX finds the key
            self._data[key] = (self._value(value), None if ex is None else now + float(ex))
            self._writes += 1
            if self._writes % self._SWEEP_EVERY == 0:
                self._sweep(now)
            return True

    def delete(self, *keys):
        now = time.monotonic()
        removed = 0
        with self._lock:
            for key in keys:
                key = self._key(key)
                if self._live(key, now) is not None:
                    del self._data[key]
                    removed += 1
        return removed

    def exists(self, *keys):
        now = time.monotonic()
        with self._lock:
            return sum(1 for key in keys if self._live(self._key(key), now) is not None)

    def ttl(self, key):
        """Seconds left, or -2 when missing / -1 when set without expiry (as Redis)."""
        key = self._key(key)
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            if entry is None:
                return -2
            expires_at = entry[1]
            if expires_at is None:
                return -1
            return int(expires_at - now)

    def eval(self, script, numkeys, *keys_and_args):
        """Only the one Lua script in the tree: INCR, and EXPIRE on first bump.

        Emulated under the same lock, so it keeps the atomicity the script was
        written for. Anything else raises rather than silently misbehaving.
        """
        normalized = " ".join(script.split())
        if 'redis.call("INCR", KEYS[1])' not in normalized or 'redis.call("EXPIRE", KEYS[1]' not in normalized:
            raise NotImplementedError("InProcessStore.eval supports only the verify-attempts INCR/EXPIRE script")
        if numkeys != 1:
            raise NotImplementedError("InProcessStore.eval supports only single-key scripts")

        key = self._key(keys_and_args[0])
        expire_seconds = float(keys_and_args[1])
        now = time.monotonic()
        with self._lock:
            entry = self._live(key, now)
            count = 1 if entry is None else int(entry[0]) + 1
            # TTL is set on the first increment only, as the script does; later
            # bumps keep the original deadline so the window is not extended.
            expires_at = (now + expire_seconds) if entry is None else entry[1]
            self._data[key] = (str(count).encode(), expires_at)
            return count


_store = InProcessStore()


def redis_instance():
    """Process-local stand-in for `redis.Redis`. Same object for every caller."""
    return _store
