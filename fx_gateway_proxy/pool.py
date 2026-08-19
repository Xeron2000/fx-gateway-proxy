import time
import random
from collections import deque
from typing import Any, Dict, List, Optional

from .config import (
    EST_RPM_LIMIT,
    EST_TPM_LIMIT,
    EST_RPM_MAX,
    EST_TPM_MAX,
    KEY_COOLDOWN_BASE,
    KEY_COOLDOWN_MAX,
    mask_key,
    resolve_keys,
)


class KeyStats:
    """Per-key sliding-window statistics and learned rate-limit estimates."""

    def __init__(self, key: str):
        self.key = key
        self.success = 0
        self.failed_429 = 0
        self.failed_other = 0
        self.backoff = 0.0
        self.cooldown_until = 0.0
        self.est_rpm = EST_RPM_LIMIT
        self.est_tpm = EST_TPM_LIMIT
        self.last_latency = 0.0
        self.window: deque = deque()  # (timestamp, tokens) pairs

    def usage(self, now: float, seconds: float = 60.0) -> tuple:
        while self.window and self.window[0][0] < now - seconds:
            self.window.popleft()
        reqs = len(self.window)
        toks = sum(t for _, t in self.window)
        return reqs, toks

    def load(self, now: float) -> float:
        reqs, toks = self.usage(now)
        return max(reqs / max(self.est_rpm, 1), toks / max(self.est_tpm, 1))


class KeyPool:
    """Adaptive key router.

    - Records per-key success/429/other failures and 60s token+request usage.
    - Learns each key's effective RPM/TPM ceiling from 429 observations and
      high-load successes, then spreads load proportionally.
    - 429 cooldown uses exponential backoff (base 30s, cap 300s), reset on success.
    - Non-429 errors (401/5xx) only lower a key's weight, no cooldown.
    """

    def __init__(self, keys: List[str]):
        self.keys: List[str] = []
        self._stats: Dict[str, KeyStats] = {}
        self.sync(keys)

    def sync(self, keys: List[str]) -> None:
        """Add new keys / drop removed keys, keeping stats for survivors."""
        self.keys = [k for k in keys if k]
        self._stats = {k: st for k, st in self._stats.items() if k in self.keys}
        for k in self.keys:
            if k not in self._stats:
                self._stats[k] = KeyStats(k)

    def next(self) -> str:
        """Pick the key with the best score: low current load, high success rate,
        plus jitter to avoid synchronized thundering. Skips cooling keys; if all
        are cooling, returns the one expiring soonest."""
        now = time.time()
        available = [k for k in self.keys if self._stats[k].cooldown_until <= now]
        if not available:
            return min(self.keys, key=lambda kk: self._stats[kk].cooldown_until)
        best, best_score = None, -1e18
        for k in available:
            st = self._stats[k]
            total = st.success + st.failed_429 + st.failed_other
            rate = st.success / total if total else 1.0
            score = (1.0 - st.load(now)) * 10.0 + rate * 3.0 + random.random()
            if score > best_score:
                best, best_score = k, score
        return best

    def mark_success(self, key: str, tokens: int = 0, latency: float = 0.0) -> None:
        st = self._stats.get(key)
        if st is None:
            return
        now = time.time()
        st.success += 1
        st.backoff = 0.0
        st.last_latency = latency
        st.window.append((now, max(tokens, 0)))
        # Surviving near the estimated ceiling means the true limit is higher:
        # raise the estimate a bit so load spreads correctly.
        reqs, toks = st.usage(now)
        if toks > 0.8 * st.est_tpm and st.est_tpm < EST_TPM_MAX:
            st.est_tpm = min(st.est_tpm * 1.1, EST_TPM_MAX)
        if reqs > 0.8 * st.est_rpm and st.est_rpm < EST_RPM_MAX:
            st.est_rpm = min(st.est_rpm * 1.1, EST_RPM_MAX)

    def mark_failed(self, key: str) -> None:
        """429: exponential-backoff cooldown + learn the ceiling from window usage."""
        st = self._stats.get(key)
        if st is None:
            return
        now = time.time()
        st.failed_429 += 1
        st.backoff = min((st.backoff * 2) or KEY_COOLDOWN_BASE, KEY_COOLDOWN_MAX)
        st.cooldown_until = now + st.backoff
        # The window usage at the 429 moment is >= the real ceiling: record a
        # conservative (0.8x) estimate so routing avoids re-tripping it.
        reqs, toks = st.usage(now)
        if toks >= 1000 and toks * 0.8 < st.est_tpm:
            st.est_tpm = max(toks * 0.8, 1000)
        if reqs >= 3 and reqs * 0.8 < st.est_rpm:
            st.est_rpm = max(reqs * 0.8, 2)

    def mark_error(self, key: str) -> None:
        """Non-429 failure (401/5xx): weight penalty only, no cooldown."""
        st = self._stats.get(key)
        if st is not None:
            st.failed_other += 1

    def stats(self) -> List[Dict[str, Any]]:
        now = time.time()
        out = []
        for k in self.keys:
            st = self._stats[k]
            reqs, toks = st.usage(now)
            out.append({
                "key": mask_key(k),
                "status": "cooldown" if st.cooldown_until > now else "active",
                "success": st.success,
                "failed_429": st.failed_429,
                "failed_other": st.failed_other,
                "requests_60s": reqs,
                "tokens_60s": toks,
                "load": round(st.load(now), 3),
                "est_rpm": round(st.est_rpm),
                "est_tpm": round(st.est_tpm),
                "backoff": round(st.backoff, 1),
                "cooldown_until": round(st.cooldown_until, 1),
                "last_latency_ms": round(st.last_latency * 1000) if st.last_latency else 0,
            })
        return out


_key_pool: Optional[KeyPool] = None


def get_key_pool(explicit_key: str = "") -> KeyPool:
    """Global pool; re-reads keys from env/file on every request so edits apply live."""
    global _key_pool
    keys = resolve_keys(explicit_key)
    if _key_pool is None:
        _key_pool = KeyPool(keys)
    else:
        _key_pool.sync(keys)
    return _key_pool
