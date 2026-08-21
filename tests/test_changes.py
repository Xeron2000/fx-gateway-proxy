"""Targeted verification for the uncommitted changes:
  1. _backoff_delay now adds jitter (non-deterministic, within [base+0.05, base+0.25])
  2. session_id is deterministic/persistent (client_ip + key suffix) when no header given
  3. providerOptions.gateway.speed=fast injected (default on; opt-out via FX_FAST_MODE=0)
  4. monolith fx-gateway-proxy.py mirrors the package behavior
  5. FX_PROXY outbound proxy defaults to direct

Run:  uv run python tests/test_changes.py
"""
import asyncio
import importlib
import json
import logging
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fx_gateway_proxy import config, pool, server


# import the monolith as a module
_MONO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fx-gateway-proxy.py")
import importlib.util
_spec = importlib.util.spec_from_file_location("fx_mono", _MONO_PATH)
mono = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mono)


class _FakeClient:
    def __init__(self, responder):
        self._res = responder
        self.received = []

    def build_request(self, *a, **kw):
        return httpx.Request("POST", "https://up", json=kw.get("json"), headers=kw.get("headers"))

    async def send(self, req, stream=False):
        self.received.append(req)
        return self._res(req)


def _sse_ok():
    ev = [{"type": "finish", "finishReason": {"unified": "stop"},
           "usage": {"raw": {"prompt_tokens": 1, "completion_tokens": 1}}}]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in ev).encode()
    return httpx.Response(200, content=body, request=httpx.Request("POST", "https://up"))


class _Base(unittest.TestCase):
    def setUp(self):
        pool._key_pool = None
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_a,vck_b"
        os.environ["AI_GATEWAY_API_KEY"] = ""
        os.environ["FX_BASE_DELAY"] = "0.0"
        os.environ["FX_MAX_DELAY"] = "0.0"
        os.environ["FX_MAX_KEY_RETRIES"] = "3"
        pool.get_key_pool("dummy")
        self._orig = server.http_client

    def tearDown(self):
        server.http_client = self._orig

    def _client(self, responder):
        fc = _FakeClient(responder)
        server.http_client = fc
        return fc

    def _app(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t")

    def _run(self, coro):
        return asyncio.run(coro)


# ── 1. backoff jitter ──
class TestBackoffJitter(_Base):
    # NOTE: server.BASE_DELAY/MAX_DELAY are bound at import time from env, so we
    # patch the module attributes directly rather than rely on setUp env vars.

    def setUp(self):
        super().setUp()
        self._ob, self._om = server.BASE_DELAY, server.MAX_DELAY
        server.BASE_DELAY = 0.8
        server.MAX_DELAY = 20.0

    def tearDown(self):
        server.BASE_DELAY, server.MAX_DELAY = self._ob, self._om
        super().tearDown()

    def test_jitter_within_range(self):
        # use a low BASE_DELAY so attempts 0-5 stay well under MAX_DELAY, isolating jitter range
        server.BASE_DELAY = 0.1
        server.MAX_DELAY = 20.0
        for a in range(6):
            d = server._backoff_delay(a)
            base = 0.1 * (2 ** a)  # 0.1,0.2,0.4,...,3.2 — all below cap
            self.assertGreaterEqual(d, base + 0.05, f"attempt {a}: {d} below base+jitter")
            self.assertLessEqual(d, base + 0.25, f"attempt {a}: {d} above jitter cap")

    def test_jitter_is_nondeterministic(self):
        vals = {server._backoff_delay(2) for _ in range(30)}
        # with jitter range [0.05,0.25], 30 samples should almost surely vary
        self.assertGreater(len(vals), 1, "jitter produced no variance")

    def test_jitter_never_exceeds_max_delay(self):
        """Regression: jitter folded in BEFORE the cap, so delay never exceeds MAX_DELAY."""
        server.BASE_DELAY = 1.0
        server.MAX_DELAY = 2.0  # low cap so attempts saturate quickly
        for a in range(10):
            d = server._backoff_delay(a)
            self.assertLessEqual(d, 2.0, f"attempt {a}: {d} exceeded MAX_DELAY")

    def test_429_log_and_sleep_use_same_value(self):
        """Regression: the backoff seconds logged on 429 must equal the actual sleep.
        Was: log called _backoff_delay() and sleep called it again -> different jitter."""
        import logging
        server.BASE_DELAY = 0.5
        server.MAX_DELAY = 20.0
        slept = []
        orig_sleep = asyncio.sleep
        async def fake_sleep(d):
            slept.append(d)
            await orig_sleep(0)  # don't actually wait
        logged = []
        class CaptureHandler(logging.Handler):
            def emit(self, rec): logged.append(rec.getMessage())
        handler = CaptureHandler()
        server.logger.addHandler(handler)
        with patch.object(asyncio, "sleep", fake_sleep):
            try:
                def res(req):
                    return httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req)
                self._client(res)
                async def go():
                    async with self._app() as c:
                        await c.post("/v1/chat/completions", json={
                            "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": "Bearer dummy"})
                self._run(go())
            finally:
                server.logger.removeHandler(handler)
        # there must be at least one 429 backoff log line and one sleep
        self.assertTrue(slept, "asyncio.sleep was never called on 429")
        backoff_logs = [l for l in logged if "backing off" in l]
        self.assertTrue(backoff_logs, f"no 'backing off' log line; logs={logged}")
        # parse the seconds from the last backoff log
        import re
        m = re.search(r"backing off ([\d.]+)s", backoff_logs[-1])
        self.assertIsNotNone(m, f"could not parse seconds from log: {backoff_logs[-1]}")
        logged_secs = float(m.group(1))
        self.assertEqual(logged_secs, slept[-1],
                         f"log said {logged_secs}s but slept {slept[-1]}s")


# ── 2. session_id persistence / determinism ──
class TestSessionIdAffinity(_Base):
    def _capturing(self):
        seen = {}
        def res(req):
            seen["sid"] = req.headers.get("x-session-id")
            seen["aff"] = req.headers.get("x-session-affinity")
            return _sse_ok()
        fc = self._client(res)
        return fc, seen

    def test_no_header_derives_deterministic_sid(self):
        """Same client+key => same session_id across two requests (no random uuid)."""
        fc, seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer vck_a"})
        self._run(go())
        sid1 = seen["sid"]
        self.assertTrue(sid1.startswith("fx-"), f"derived sid should be fx-prefixed, got {sid1}")
        self.assertEqual(seen["aff"], sid1, "affinity header must mirror session-id")

    def test_same_client_same_key_stable_sid(self):
        """Same client+key must yield identical session_id across requests (no uuid4)."""
        sids = []
        def res(req):
            sids.append(req.headers.get("x-session-id"))
            return _sse_ok()
        self._client(res)
        async def go():
            async with self._app() as c:
                for _ in range(3):
                    await c.post("/v1/chat/completions", json={
                        "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": "Bearer vck_a"})
        self._run(go())
        self.assertEqual(len(set(sids)), 1, f"session_id not stable: {sids}")
        self.assertTrue(sids[0].startswith("fx-"))

    def test_different_key_yields_different_sid(self):
        """Different API key => different session affinity (key-suffix in hash)."""
        results = {}
        def make(key):
            def res(req):
                results[key] = req.headers.get("x-session-id")
                return _sse_ok()
            return res
        async def go():
            async with self._app() as c:
                self._client(make("vck_a"))
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer vck_a"})
                self._client(make("vck_b"))
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer vck_b"})
        self._run(go())
        self.assertNotEqual(results["vck_a"], results["vck_b"])

    def test_explicit_header_wins(self):
        fc, seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer vck_a", "x-session-id": "my-session"})
        self._run(go())
        self.assertEqual(seen["sid"], "my-session")


# ── 3. providerOptions speed=fast ──
class TestFastMode(_Base):
    def _capturing(self):
        seen = {}
        def res(req):
            seen["payload"] = json.loads(req.content)
            return _sse_ok()
        self._client(res)
        return seen

    def test_fast_injected_by_default(self):
        os.environ.pop("FX_FAST_MODE", None)
        seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer dummy"})
        self._run(go())
        self.assertEqual(seen["payload"].get("providerOptions"), {"gateway": {"speed": "fast"}})

    def test_fast_explicit_speed_param(self):
        os.environ["FX_FAST_MODE"] = "0"
        seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "speed": "fast",
                    "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer dummy"})
        self._run(go())
        self.assertEqual(seen["payload"].get("providerOptions"), {"gateway": {"speed": "fast"}})

    def test_fast_model_suffix(self):
        os.environ["FX_FAST_MODE"] = "0"
        seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2-fast", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer dummy"})
        self._run(go())
        self.assertEqual(seen["payload"].get("providerOptions"), {"gateway": {"speed": "fast"}})

    def test_fast_disabled_when_env_off(self):
        os.environ["FX_FAST_MODE"] = "0"
        seen = self._capturing()
        async def go():
            async with self._app() as c:
                await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer dummy"})
        self._run(go())
        self.assertNotIn("providerOptions", seen["payload"], "fast should NOT be injected when disabled")


# ── 5. proxy authentication ──
class TestProxyAuth(_Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("PROXY_API_KEY", None)
        os.environ.pop("PROXY_API_KEYS", None)

    def tearDown(self):
        os.environ.pop("PROXY_API_KEY", None)
        os.environ.pop("PROXY_API_KEYS", None)
        super().tearDown()

    def test_no_proxy_key_allows_dummy(self):
        """When PROXY_API_KEY is unset, requests with dummy auth succeed."""
        self._client(lambda req: _sse_ok())
        async def go():
            async with self._app() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer dummy"})
                self.assertEqual(r.status_code, 200)
        self._run(go())

    def test_proxy_key_blocks_unauthorized_requests(self):
        """When PROXY_API_KEY is set, missing or wrong auth returns 401."""
        os.environ["PROXY_API_KEY"] = "secret-pass"
        self._client(lambda req: _sse_ok())
        async def go():
            async with self._app() as c:
                # 1. No auth
                r1 = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(r1.status_code, 401)
                self.assertIn("Proxy API Key", r1.text)

                # 2. Wrong auth
                r2 = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer wrong-pass"})
                self.assertEqual(r2.status_code, 401)

                # 3. Stats blocked without auth
                r3 = await c.get("/v1/stats")
                self.assertEqual(r3.status_code, 401)

                # 4. Health endpoint remains public
                r4 = await c.get("/health")
                self.assertEqual(r4.status_code, 200)
        self._run(go())

    def test_proxy_key_valid_authenticates_and_uses_pool(self):
        """Valid proxy key passes auth and routes through upstream pool."""
        os.environ["PROXY_API_KEY"] = "secret-pass"
        seen_upstream_auth = []
        def res(req):
            seen_upstream_auth.append(req.headers.get("authorization"))
            return _sse_ok()
        self._client(res)
        async def go():
            async with self._app() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer secret-pass"})
                self.assertEqual(r.status_code, 200)

                # Anthropic messages with x-api-key
                r_anthro = await c.post("/v1/messages", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
                    headers={"x-api-key": "secret-pass", "anthropic-version": "2023-06-01"})
                self.assertEqual(r_anthro.status_code, 200)

                # Stats with valid auth
                r_stats = await c.get("/v1/stats", headers={"Authorization": "Bearer secret-pass"})
                self.assertEqual(r_stats.status_code, 200)
                self.assertEqual(r_stats.json()["total"], 2)
        self._run(go())
        # Upstream requests must receive real upstream keys (vck_a or vck_b), NOT proxy key!
        for auth in seen_upstream_auth:
            self.assertTrue(auth.startswith("Bearer vck_"), f"Upstream received wrong key: {auth}")

    def test_multi_proxy_keys_allowed(self):
        """Multiple comma-separated PROXY_API_KEYS are all valid."""
        os.environ["PROXY_API_KEYS"] = "key1,key2"
        self._client(lambda req: _sse_ok())
        async def go():
            async with self._app() as c:
                r1 = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer key1"})
                self.assertEqual(r1.status_code, 200)

                r2 = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer key2"})
                self.assertEqual(r2.status_code, 200)
        self._run(go())


# ── 6. outbound FX_PROXY ──
class TestOutboundProxy(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("FX_PROXY")
        os.environ.pop("FX_PROXY", None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FX_PROXY", None)
        else:
            os.environ["FX_PROXY"] = self._old

    def test_unset_is_direct(self):
        self.assertIsNone(config.get_proxy_url())
        self.assertIsNone(mono.get_proxy_url())

    def test_fx_proxy_url(self):
        os.environ["FX_PROXY"] = "http://127.0.0.1:7890"
        self.assertEqual(config.get_proxy_url(), "http://127.0.0.1:7890")
        self.assertEqual(mono.get_proxy_url(), "http://127.0.0.1:7890")

    def test_fx_proxy_none(self):
        os.environ["FX_PROXY"] = "none"
        self.assertIsNone(config.get_proxy_url())
        self.assertIsNone(mono.get_proxy_url())

    def test_bare_host_gets_http_scheme(self):
        os.environ["FX_PROXY"] = "127.0.0.1:7890"
        self.assertEqual(config.get_proxy_url(), "http://127.0.0.1:7890")


# ── 4. monolith parity ──
class TestMonolithParity(unittest.TestCase):
    def test_mono_backoff_has_jitter(self):
        vals = {mono._backoff_delay(0) for _ in range(20)}
        self.assertGreater(len(vals), 1, "monolith _backoff_delay has no jitter")

    def test_mono_backoff_respects_max_delay(self):
        """Monolith _backoff_delay must cap at MAX_DELAY (parity with package)."""
        mono.BASE_DELAY = 1.0
        mono.MAX_DELAY = 2.0
        for a in range(10):
            self.assertLessEqual(mono._backoff_delay(a), 2.0, f"mono attempt {a} exceeded MAX_DELAY")

    def test_mono_user_agent_matches(self):
        self.assertEqual(mono.USER_AGENT, config.USER_AGENT)

    def test_mono_has_fast_injection(self):
        import inspect
        src = inspect.getsource(mono.create_app)
        self.assertIn("providerOptions", src)
        self.assertIn('gateway', src)
        self.assertIn("speed", src)

    def test_mono_has_session_affinity(self):
        import inspect
        src = inspect.getsource(mono.create_app)
        self.assertIn("hashlib.md5", src)
        self.assertIn("auth_suffix", src)

    def test_mono_has_proxy_auth(self):
        import inspect
        src = inspect.getsource(mono)
        self.assertIn("PROXY_API_KEYS", src)
        self.assertIn("get_proxy_keys", src)
        self.assertIn("_check_auth", src)

    def test_mono_has_outbound_proxy(self):
        import inspect
        src = inspect.getsource(mono)
        self.assertIn("FX_PROXY", src)
        self.assertIn("get_proxy_url", src)
        self.assertIn("trust_env", inspect.getsource(mono.create_http_client))


if __name__ == "__main__":
    unittest.main(verbosity=2)

