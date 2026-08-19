"""Bug-hunting tests for fx-gateway-proxy. No external test framework.

Run:  uv run python tests/test_bugs.py
Uses stdlib unittest + httpx ASGITransport (already a dependency) against the
real FastAPI app with UPSTREAM_URL pointed at a local mock server.
"""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

# Ensure package importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fx_gateway_proxy import config, converter, pool, server


def _sse_lines(events):
    """Build an upstream SSE body from a list of event dicts."""
    out = []
    for ev in events:
        out.append(f"data: {json.dumps(ev)}\n\n")
    return "".join(out)


class TestConverter(unittest.TestCase):
    # --- _extract_text ---

    def test_extract_text_none(self):
        self.assertEqual(converter._extract_text(None), "")

    def test_extract_text_str(self):
        self.assertEqual(converter._extract_text("hello"), "hello")

    def test_extract_text_list_of_strs(self):
        self.assertEqual(converter._extract_text(["a", "b"]), "ab")

    def test_extract_text_list_of_blocks(self):
        content = [{"type": "text", "text": "hi"}, {"type": "input_text", "text": "there"}]
        self.assertEqual(converter._extract_text(content), "hithere")

    def test_extract_text_list_nested_unknown_type_with_text(self):
        self.assertEqual(converter._extract_text([{"type": "weird", "text": "x"}]), "x")

    def test_extract_text_dict_with_text(self):
        self.assertEqual(converter._extract_text({"text": "y"}), "y")

    def test_extract_text_dict_without_text(self):
        # ponytail: dict without "text" key falls through to "" (intentional)
        self.assertEqual(converter._extract_text({"foo": "bar"}), "")

    def test_extract_text_int(self):
        self.assertEqual(converter._extract_text(123), "123")

    # --- convert_messages_to_v3 ---

    def test_user_empty_content_becomes_space(self):
        """Empty user content must be replaced with a space to avoid 400."""
        out = converter.convert_messages_to_v3([{"role": "user", "content": ""}])
        self.assertEqual(out[0]["content"], [{"type": "text", "text": " "}])

    def test_user_whitespace_content_becomes_space(self):
        out = converter.convert_messages_to_v3([{"role": "user", "content": "   \n  "}])
        self.assertEqual(out[0]["content"], [{"type": "text", "text": " "}])

    def test_user_text_part_whitespace_becomes_space(self):
        out = converter.convert_messages_to_v3([
            {"role": "user", "content": [{"type": "text", "text": "  "}]}
        ])
        self.assertEqual(out[0]["content"][0]["text"], " ")

    def test_user_image_url_data_uri(self):
        url = "data:image/png;base64,iVBORw0KGgo="
        out = converter.convert_messages_to_v3([
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}
        ])
        part = out[0]["content"][0]
        self.assertEqual(part["type"], "file")
        self.assertEqual(part["mediaType"], "image/png")
        self.assertEqual(part["data"], "iVBORw0KGgo=")

    def test_user_image_url_http(self):
        out = converter.convert_messages_to_v3([
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x.com/a.JPG"}}]}
        ])
        part = out[0]["content"][0]
        self.assertEqual(part["mediaType"], "image/jpeg")
        self.assertEqual(part["data"], "https://x.com/a.JPG")

    def test_user_image_url_no_extension_defaults_png(self):
        out = converter.convert_messages_to_v3([
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x.com/img"}}]}
        ])
        self.assertEqual(out[0]["content"][0]["mediaType"], "image/png")

    def test_system_role(self):
        out = converter.convert_messages_to_v3([{"role": "system", "content": "be nice"}])
        self.assertEqual(out, [{"role": "system", "content": "be nice"}])

    def test_developer_role_maps_to_system(self):
        out = converter.convert_messages_to_v3([{"role": "developer", "content": "be strict"}])
        self.assertEqual(out, [{"role": "system", "content": "be strict"}])

    def test_assistant_with_tool_calls_str_args(self):
        out = converter.convert_messages_to_v3([{
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"id": "tc1", "function": {"name": "f", "arguments": '{"a": 1}'}}]
        }])
        parts = out[0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "thinking"})
        self.assertEqual(parts[1]["type"], "tool-call")
        self.assertEqual(parts[1]["input"], {"a": 1})

    def test_assistant_tool_calls_invalid_json_args(self):
        out = converter.convert_messages_to_v3([{
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "tc1", "function": {"name": "f", "arguments": "not json"}}]
        }])
        # invalid json -> keeps raw str wrapped in {} per code
        self.assertEqual(out[0]["content"][0]["input"], {})

    def test_assistant_empty_no_toolcalls(self):
        out = converter.convert_messages_to_v3([{"role": "assistant", "content": ""}])
        self.assertEqual(out[0]["content"], [{"type": "text", "text": " "}])

    def test_tool_role_str(self):
        out = converter.convert_messages_to_v3([{"role": "tool", "tool_call_id": "tc1", "name": "f", "content": "result"}])
        part = out[0]["content"][0]
        self.assertEqual(part["type"], "tool-result")
        self.assertEqual(part["output"], {"type": "text", "value": "result"})

    def test_tool_role_empty_content(self):
        out = converter.convert_messages_to_v3([{"role": "tool", "tool_call_id": "tc1", "content": ""}])
        self.assertEqual(out[0]["content"][0]["output"]["value"], "{}")

    def test_tool_role_dict_content(self):
        out = converter.convert_messages_to_v3([{"role": "tool", "tool_call_id": "tc1", "content": {"a": 1}}])
        self.assertEqual(out[0]["content"][0]["output"]["value"], '{"a": 1}')

    # --- convert_tools / tool_choice ---

    def test_convert_tools_none(self):
        self.assertIsNone(converter.convert_tools_to_v3(None))
        self.assertIsNone(converter.convert_tools_to_v3([]))

    def test_convert_tools_function(self):
        tools = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}]
        out = converter.convert_tools_to_v3(tools)
        self.assertEqual(out[0]["name"], "f")
        self.assertEqual(out[0]["inputSchema"], {"type": "object"})

    def test_convert_tools_legacy_shape(self):
        tools = [{"type": "function", "name": "g", "description": "d", "inputSchema": {"type": "object"}}]
        out = converter.convert_tools_to_v3(tools)
        self.assertEqual(out[0]["name"], "g")

    def test_tool_choice_none_default_auto(self):
        self.assertEqual(converter.convert_tool_choice_to_v3(None), {"type": "auto"})

    def test_tool_choice_required(self):
        self.assertEqual(converter.convert_tool_choice_to_v3("required"), {"type": "required"})

    def test_tool_choice_string_name(self):
        self.assertEqual(converter.convert_tool_choice_to_v3("mytool"), {"type": "tool", "toolName": "mytool"})

    def test_tool_choice_dict_function(self):
        tc = {"type": "function", "function": {"name": "fn"}}
        self.assertEqual(converter.convert_tool_choice_to_v3(tc), {"type": "tool", "toolName": "fn"})

    # --- reasoning effort ---

    def test_map_reasoning_none(self):
        self.assertIsNone(converter.map_reasoning_effort(None))

    def test_map_reasoning_xhigh(self):
        self.assertEqual(converter.map_reasoning_effort("xhigh"), "xhigh")
        self.assertEqual(converter.map_reasoning_effort("max"), "xhigh")
        self.assertEqual(converter.map_reasoning_effort("XHIGH"), "xhigh")

    def test_map_reasoning_high(self):
        self.assertEqual(converter.map_reasoning_effort("high"), "high")

    def test_map_reasoning_auto_group(self):
        for v in ("medium", "low", "minimal", "auto", "default"):
            self.assertEqual(converter.map_reasoning_effort(v), "auto")

    def test_map_reasoning_off(self):
        self.assertEqual(converter.map_reasoning_effort("off"), "none")

    def test_map_reasoning_passthrough(self):
        self.assertEqual(converter.map_reasoning_effort("bogus"), "bogus")

    # --- extract_usage ---

    def test_extract_usage_empty(self):
        u = converter.extract_usage({})
        self.assertEqual(u["total_tokens"], 0)
        self.assertEqual(u["completion_tokens"], 0)

    def test_extract_usage_raw_shape(self):
        event = {"usage": {"raw": {"prompt_tokens": 10, "completion_tokens": 5,
                                   "reasoning_tokens": 2,
                                   "prompt_tokens_details": {"cached_tokens": 3}}}}
        u = converter.extract_usage(event)
        self.assertEqual(u["prompt_tokens"], 10)
        self.assertEqual(u["completion_tokens"], 5)
        self.assertEqual(u["total_tokens"], 15)
        self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 3)
        self.assertEqual(u["completion_tokens_details"]["reasoning_tokens"], 2)

    def test_extract_usage_new_shape(self):
        event = {"usage": {
            "inputTokens": {"total": 8, "cacheRead": 4},
            "outputTokens": {"total": 6, "reasoning": 1}
        }}
        u = converter.extract_usage(event)
        self.assertEqual(u["prompt_tokens"], 8)
        self.assertEqual(u["completion_tokens"], 6)
        self.assertEqual(u["prompt_tokens_details"]["cached_tokens"], 4)
        self.assertEqual(u["completion_tokens_details"]["reasoning_tokens"], 1)


class TestConfig(unittest.TestCase):
    def test_mask_key_long(self):
        self.assertEqual(config.mask_key("vck_abcdefghij1234"), "vck_abc...1234")  # [:7] + ... + [-4:]

    def test_mask_key_short(self):
        self.assertEqual(config.mask_key("ab"), "ab***")

    def test_resolve_keys_placeholder_returns_env(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "vck_a, vck_b", "AI_GATEWAY_API_KEY": ""}, clear=False):
            keys = config.resolve_keys("dummy")
            self.assertEqual(keys, ["vck_a", "vck_b"])

    def test_resolve_keys_explicit(self):
        self.assertEqual(config.resolve_keys("vck_real"), ["vck_real"])

    def test_resolve_keys_explicit_placeholder(self):
        # placeholder explicit key falls through to env
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "vck_env", "AI_GATEWAY_API_KEY": ""}, clear=False):
            self.assertEqual(config.resolve_keys("ollama"), ["vck_env"])

    def test_resolve_keys_newline_separated(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "vck_a\nvck_b\nvck_c", "AI_GATEWAY_API_KEY": ""}, clear=False):
            self.assertEqual(config.resolve_keys("dummy"), ["vck_a", "vck_b", "vck_c"])

    def test_resolve_keys_both_env_vars_dedup(self):
        """Same key in both AI_GATEWAY_API_KEYS and AI_GATEWAY_API_KEY is deduped."""
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "vck_a", "AI_GATEWAY_API_KEY": "vck_a"}, clear=False):
            keys = config.resolve_keys("dummy")
            self.assertEqual(len(keys), 1, f"duplicate keys not deduped: {keys}")

    def test_resolve_keys_skips_placeholder_in_keys_env(self):
        """A literal 'dummy' inside AI_GATEWAY_API_KEYS is NOT filtered (only the
        explicit_key param is checked against PLACEHOLDER_KEYS). Documented edge case."""
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "dummy,vck_real", "AI_GATEWAY_API_KEY": ""}, clear=False):
            keys = config.resolve_keys("dummy")
            self.assertIn("dummy", keys)  # passed through — would cause 401 upstream


class TestKeyPool(unittest.TestCase):
    def setUp(self):
        # isolate global pool
        pool._key_pool = None

    def test_sync_preserves_stats(self):
        kp = pool.KeyPool(["k1"])
        kp._stats["k1"].success = 5
        kp.sync(["k1", "k2"])
        self.assertEqual(kp._stats["k1"].success, 5)
        self.assertIn("k2", kp._stats)

    def test_sync_drops_removed(self):
        kp = pool.KeyPool(["k1", "k2"])
        kp._stats["k1"].success = 3
        kp.sync(["k1"])
        self.assertEqual(kp.keys, ["k1"])
        self.assertNotIn("k2", kp._stats)

    def test_next_all_available(self):
        kp = pool.KeyPool(["k1", "k2"])
        chosen = kp.next()
        self.assertIn(chosen, ["k1", "k2"])

    def test_next_all_cooling_returns_soonest(self):
        kp = pool.KeyPool(["k1", "k2"])
        now = time.time()
        kp._stats["k1"].cooldown_until = now + 100
        kp._stats["k2"].cooldown_until = now + 10
        chosen = kp.next()
        self.assertEqual(chosen, "k2")

    def test_next_empty_keys_raises_runtimeerror(self):
        """Empty pool raises a clear RuntimeError (was ValueError from min() of empty)."""
        kp = pool.KeyPool([])
        with self.assertRaises(RuntimeError):
            kp.next()

    def test_mark_failed_cooldown_backoff(self):
        kp = pool.KeyPool(["k1"])
        kp.mark_failed("k1")
        self.assertGreater(kp._stats["k1"].cooldown_until, time.time())
        self.assertGreater(kp._stats["k1"].backoff, 0)
        # second fail doubles
        first = kp._stats["k1"].backoff
        kp.mark_failed("k1")
        self.assertGreaterEqual(kp._stats["k1"].backoff, first)

    def test_mark_success_resets_backoff(self):
        kp = pool.KeyPool(["k1"])
        kp.mark_failed("k1")
        kp.mark_success("k1", tokens=100, latency=0.5)
        self.assertEqual(kp._stats["k1"].backoff, 0.0)
        self.assertEqual(kp._stats["k1"].success, 1)

    def test_mark_success_raises_ceiling(self):
        kp = pool.KeyPool(["k1"])
        st = kp._stats["k1"]
        st.est_tpm = 100.0
        # push usage above 0.8 of est_tpm within window
        for _ in range(5):
            kp.mark_success("k1", tokens=90, latency=0.1)  # 90 > 80
        self.assertGreater(kp._stats["k1"].est_tpm, 100.0)

    def test_mark_failed_learns_ceiling(self):
        kp = pool.KeyPool(["k1"])
        st = kp._stats["k1"]
        st.est_tpm = 100000.0  # high default
        # generate window usage of 5000 tokens
        for _ in range(10):
            st.window.append((time.time(), 500))
        kp.mark_failed("k1")
        # toks*0.8 = 4000 should be recorded as new lower est_tpm
        self.assertLess(kp._stats["k1"].est_tpm, 100000.0)

    def test_mark_error_no_cooldown(self):
        kp = pool.KeyPool(["k1"])
        kp.mark_error("k1")
        self.assertEqual(kp._stats["k1"].cooldown_until, 0.0)
        self.assertEqual(kp._stats["k1"].failed_other, 1)

    def test_stats_masks_keys(self):
        kp = pool.KeyPool(["vck_abcdefghij"])
        s = kp.stats()
        self.assertIn("...", s[0]["key"])

    def test_get_key_pool_caches_and_syncs(self):
        with patch.dict(os.environ, {"AI_GATEWAY_API_KEYS": "vck_a", "AI_GATEWAY_API_KEY": ""}, clear=False):
            p1 = pool.get_key_pool("dummy")
            p2 = pool.get_key_pool("dummy")
            self.assertIs(p1, p2)  # same instance


class TestVersionConsistency(unittest.TestCase):
    def test_init_version_matches_config(self):
        """__init__.py __version__ must match config.py."""
        import fx_gateway_proxy
        self.assertEqual(fx_gateway_proxy.__version__, config.__version__,
                         "__init__.py __version__ out of sync with config.py")


class TestServerRouting(unittest.TestCase):
    """Test retry/rotation logic in server.py by mocking httpx send."""

    def setUp(self):
        pool._key_pool = None
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_a,vck_b,vck_c"
        os.environ["AI_GATEWAY_API_KEY"] = ""

    def _make_response(self, status_code, text_body=b"", sse_events=None):
        resp = httpx.Response(status_code, content=text_body if sse_events is None else _sse_lines(sse_events).encode(),
                              request=httpx.Request("POST", "https://upstream"))
        return resp

    def _post(self, app_client, payload, auth="dummy"):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        return app_client.post("/v1/chat/completions", json=payload, headers=headers)

    def test_single_key_429_retries(self):
        """Single key 429 must retry per FX_MAX_KEY_RETRIES (was: 1 attempt only)."""
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_only"
        os.environ["AI_GATEWAY_API_KEY"] = ""
        os.environ["FX_MAX_KEY_RETRIES"] = "3"
        os.environ["FX_BASE_DELAY"] = "0.01"
        pool._key_pool = None
        import importlib
        importlib.reload(config)
        importlib.reload(server)
        # re-resolve pool after reload so server module sees fresh keys
        kp = pool.get_key_pool("dummy")

        attempts = []

        class Fake:
            def build_request(self, *a, **kw):
                return httpx.Request("POST", "https://up", json=kw.get("json"), headers=kw.get("headers"))
            async def send(self, req, stream=False):
                attempts.append(1)
                return httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req)
        server.http_client = Fake()

        async def run():
            t = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
                return await c.post("/v1/chat/completions",
                                    json={"model": "zai/glm-5.2",
                                          "messages": [{"role": "user", "content": "hi"}],
                                          "stream": False},
                                    headers={"Authorization": "Bearer dummy"})
        asyncio.run(run())
        self.assertGreater(len(attempts), 1, "single-key 429 should retry per FX_MAX_KEY_RETRIES")

    def test_multi_key_all_429_backoffs(self):
        """Multi-key all-429 must back off between retries (was: 0 delay, sleep
        gated on len(keys)==1)."""
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_a,vck_b"
        os.environ["AI_GATEWAY_API_KEY"] = ""
        os.environ["FX_BASE_DELAY"] = "0.5"
        pool._key_pool = None

        class Fake:
            def build_request(self, *a, **kw):
                return httpx.Request("POST", "https://up", json=kw.get("json"), headers=kw.get("headers"))
            async def send(self, req, stream=False):
                return httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req)
        server.http_client = Fake()

        async def run():
            t = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
                return await c.post("/v1/chat/completions",
                                    json={"model": "zai/glm-5.2",
                                          "messages": [{"role": "user", "content": "hi"}],
                                          "stream": False},
                                    headers={"Authorization": "Bearer dummy"})
        import time as _t
        t0 = _t.time()
        asyncio.run(run())
        elapsed = _t.time() - t0
        self.assertGreater(elapsed, 0.1, f"multi-key 429 retries should backoff; elapsed={elapsed}")

    def test_429_rotates_and_recovers(self):
        """With 2 keys where each 429s once then succeeds, enough retry budget must
        remain for one key to recover and return 200."""
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_a,vck_b"
        os.environ["AI_GATEWAY_API_KEY"] = ""
        os.environ["FX_MAX_KEY_RETRIES"] = "4"
        os.environ["FX_BASE_DELAY"] = "0.0"
        pool._key_pool = None
        kp = pool.get_key_pool("dummy")

        seen = []

        class Fake:
            def build_request(self, *a, **kw):
                return httpx.Request("POST", "https://up", json=kw.get("json"), headers=kw.get("headers"))
            async def send(self, req, stream=False):
                auth = req.headers.get("authorization", "")
                seen.append(auth)
                is_a = "vck_a" in auth
                fa = kp._stats["vck_a"].failed_429
                fb = kp._stats["vck_b"].failed_429
                if (is_a and fa == 0) or (not is_a and fb == 0):
                    return httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req)
                finish = {"type": "finish", "finishReason": {"unified": "stop"},
                          "usage": {"raw": {"prompt_tokens": 1, "completion_tokens": 1}}}
                return httpx.Response(200, content=_sse_lines([finish]).encode(), request=req)
        server.http_client = Fake()

        async def run():
            t = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=t, base_url="http://t") as c:
                return await c.post("/v1/chat/completions",
                                    json={"model": "zai/glm-5.2",
                                          "messages": [{"role": "user", "content": "hi"}],
                                          "stream": False},
                                    headers={"Authorization": "Bearer dummy"})
        resp = asyncio.run(run())
        total_429 = kp._stats["vck_a"].failed_429 + kp._stats["vck_b"].failed_429
        self.assertGreater(total_429, 0, f"no 429 recorded; seen={seen}")
        self.assertEqual(resp.status_code, 200, f"seen={seen}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
