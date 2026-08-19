"""End-to-end mock tests for fx-gateway-proxy protocol converters and error mapping.

Uses httpx ASGITransport against the real FastAPI app with a mock upstream
(httpx client replaced). Covers streaming converters, error mapping, and
edge cases that are hard to hit against the real upstream.

Run:  uv run python tests/test_e2e_mock.py
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from fx_gateway_proxy import config, pool, server


def _sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class _FakeClient:
    """Replaces server.http_client. Returns canned SSE per-request via a callable."""
    def __init__(self, responder):
        self._responder = responder  # callable(req) -> httpx.Response

    def build_request(self, *a, **kw):
        return httpx.Request("POST", "https://up", json=kw.get("json"), headers=kw.get("headers"))

    async def send(self, req, stream=False):
        return self._responder(req)


def _sse_ok(extra=None):
    events = list(extra or [])
    events.append({"type": "finish", "finishReason": {"unified": "stop"},
                   "usage": {"raw": {"prompt_tokens": 5, "completion_tokens": 7}}})
    return httpx.Response(200, content=_sse(events), request=httpx.Request("POST", "https://up"))


def _sse_text_then_finish(text_parts, reasoning_parts=None, finish="stop"):
    events = []
    for t in (reasoning_parts or []):
        events.append({"type": "reasoning-delta", "delta": t})
    for t in text_parts:
        events.append({"type": "text-delta", "delta": t})
    events.append({"type": "finish", "finishReason": {"unified": finish},
                   "usage": {"raw": {"prompt_tokens": 5, "completion_tokens": sum(len(p) for p in text_parts)}}})
    return httpx.Response(200, content=_sse(events), request=httpx.Request("POST", "https://up"))


def _sse_tool_then_finish():
    events = [
        {"type": "tool-input-start", "id": "tc1", "toolName": "get_weather"},
        {"type": "tool-input-delta", "id": "tc1", "delta": '{"city":'},
        {"type": "tool-input-delta", "id": "tc1", "delta": ' "Paris"}'},
        {"type": "finish", "finishReason": {"unified": "tool-calls"},
         "usage": {"raw": {"prompt_tokens": 5, "completion_tokens": 10}}},
    ]
    return httpx.Response(200, content=_sse(events), request=httpx.Request("POST", "https://up"))


class _Base(unittest.TestCase):
    def setUp(self):
        pool._key_pool = None
        os.environ["AI_GATEWAY_API_KEYS"] = "vck_a,vck_b"
        os.environ["AI_GATEWAY_API_KEY"] = ""
        os.environ["FX_BASE_DELAY"] = "0.0"
        os.environ["FX_MAX_DELAY"] = "0.0"
        os.environ["FX_MAX_KEY_RETRIES"] = "3"
        # ensure a fresh pool reads the env keys
        pool.get_key_pool("dummy")
        self._orig_client = server.http_client

    def tearDown(self):
        server.http_client = self._orig_client

    def _set_client(self, responder):
        server.http_client = _FakeClient(responder)

    def _app_client(self):
        t = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=t, base_url="http://t")

    def _run(self, coro):
        return asyncio.run(coro)


class TestChatProtocol(_Base):
    def test_non_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["pong"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 10})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["object"], "chat.completion")
        self.assertEqual(d["choices"][0]["message"]["content"], "pong")
        self.assertEqual(d["choices"][0]["finish_reason"], "stop")
        self.assertEqual(d["model"], "zai/glm-5.2")
        self.assertIn("usage", d)

    def test_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["Hello", " world"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 10})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        chunks = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: ") and line[6:].strip() not in ("[DONE]", "DONE")]
        contents = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"][0]["delta"].get("content"))
        self.assertEqual(contents, "Hello world")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(r.text.rstrip().endswith("data: [DONE]"))

    def test_stream_reasoning_emitted(self):
        self._set_client(lambda req: _sse_text_then_finish(["ans"], reasoning_parts=["think"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        self.assertIn("reasoning_content", r.text)

    def test_tool_call_stream(self):
        self._set_client(lambda req: _sse_tool_then_finish())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "stream": True,
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [{"type": "function", "function": {"name": "get_weather",
                               "description": "d", "parameters": {"type": "object"}}}]})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        self.assertIn("tool_calls", r.text)
        # finish_reason tool_calls
        self.assertIn('"finish_reason": "tool_calls"', r.text)

    def test_tool_call_non_stream(self):
        self._set_client(lambda req: _sse_tool_then_finish())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2",
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [{"type": "function", "function": {"name": "get_weather",
                               "description": "d", "parameters": {"type": "object"}}}]})
                return r
        r = self._run(go())
        d = r.json()
        self.assertEqual(d["choices"][0]["finish_reason"], "tool_calls")
        tc = d["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"city": "Paris"})

    def test_error_passthrough_400(self):
        self._set_client(lambda req: httpx.Response(400, content=b'{"error":{"message":"bad prompt"}}', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": []})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400)
        d = r.json()
        self.assertEqual(d["error"]["type"], "upstream_error")
        self.assertEqual(d["error"]["code"], 400)

    def test_429_retries_then_200(self):
        calls = {"n": 0}
        def resp(req):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req)
            return _sse_text_then_finish(["ok"])
        self._set_client(resp)
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(calls["n"], 3)

    def test_all_429_final_429(self):
        self._set_client(lambda req: httpx.Response(429, content=b'{"error":{"message":"rate"}}', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}], "stream": False})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 429)


class TestResponsesProtocol(_Base):
    def test_non_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["pong"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={
                    "model": "zai/glm-5.2", "input": "say pong",
                    "max_output_tokens": 10})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["object"], "response")
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["output_text"], "pong")
        self.assertIn("usage", d)

    def test_non_stream_reasoning_and_text(self):
        self._set_client(lambda req: _sse_text_then_finish(["answer"], reasoning_parts=["think1"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={
                    "model": "zai/glm-5.2", "input": "x", "max_output_tokens": 50})
                return r
        r = self._run(go())
        d = r.json()
        types = [o["type"] for o in d["output"]]
        self.assertIn("reasoning", types)
        self.assertIn("message", types)

    def test_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["Hello", " world"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={
                    "model": "zai/glm-5.2", "input": "x", "stream": True,
                    "max_output_tokens": 10})
                return r
        r = self._run(go())
        events = [l for l in r.text.splitlines() if l.startswith("event:")]
        ev_types = [l.split(":", 1)[1].strip() for l in events]
        self.assertIn("response.created", ev_types)
        self.assertIn("response.output_text.delta", ev_types)
        self.assertIn("response.completed", ev_types)
        # deltas carry the text
        self.assertIn("Hello", r.text)
        self.assertIn(" world", r.text)

    def test_stream_tool_call(self):
        self._set_client(lambda req: _sse_tool_then_finish())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={
                    "model": "zai/glm-5.2", "input": "weather", "stream": True,
                    "tools": [{"type": "function", "name": "get_weather",
                               "description": "d", "parameters": {"type": "object"}}]})
                return r
        r = self._run(go())
        self.assertIn("response.function_call_arguments.delta", r.text)
        self.assertIn("response.function_call_arguments.done", r.text)
        self.assertIn("get_weather", r.text)

    def test_error_passthrough(self):
        self._set_client(lambda req: httpx.Response(400, content=b'{"error":{"message":"bad"}}', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={"model": "zai/glm-5.2", "input": ""})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400)

    def test_instructions_become_system(self):
        """instructions param must prepend a system message upstream."""
        seen = {}
        def responder(req):
            body = json.loads(req.content)
            seen["messages"] = body.get("prompt") or body.get("messages")
            return _sse_text_then_finish(["ok"])
        self._set_client(responder)
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", json={
                    "model": "zai/glm-5.2", "input": "hi",
                    "instructions": "be brief", "max_output_tokens": 10})
                return r
        self._run(go())
        # upstream v3 payload uses 'prompt' with role system first
        roles = [m.get("role") for m in seen["messages"]]
        self.assertEqual(roles[0], "system")


class TestAnthropicProtocol(_Base):
    def test_non_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["pong"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["type"], "message")
        self.assertEqual(d["role"], "assistant")
        text_blocks = [b["text"] for b in d["content"] if b.get("type") == "text"]
        self.assertEqual("".join(text_blocks), "pong")
        self.assertEqual(d["stop_reason"], "end_turn")

    def test_non_stream_reasoning_becomes_thinking(self):
        self._set_client(lambda req: _sse_text_then_finish(["ans"], reasoning_parts=["thought"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        d = r.json()
        types = [b["type"] for b in d["content"]]
        self.assertIn("thinking", types)
        self.assertIn("text", types)

    def test_non_stream_tool_use(self):
        self._set_client(lambda req: _sse_tool_then_finish())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 50,
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [{"name": "get_weather", "description": "d",
                               "input_schema": {"type": "object"}}]})
                return r
        r = self._run(go())
        d = r.json()
        self.assertEqual(d["stop_reason"], "tool_use")
        tool_blocks = [b for b in d["content"] if b.get("type") == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "get_weather")
        self.assertEqual(tool_blocks[0]["input"], {"city": "Paris"})

    def test_stream_basic(self):
        self._set_client(lambda req: _sse_text_then_finish(["Hello", " world"]))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 10, "stream": True,
                    "thinking": {"type": "disabled"},
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        ev_types = [l.split(":", 1)[1].strip() for l in r.text.splitlines() if l.startswith("event:")]
        self.assertIn("message_start", ev_types)
        self.assertIn("content_block_start", ev_types)
        self.assertIn("content_block_delta", ev_types)
        self.assertIn("content_block_stop", ev_types)
        self.assertIn("message_delta", ev_types)
        self.assertIn("message_stop", ev_types)

    def test_stream_tool_use_indices(self):
        self._set_client(lambda req: _sse_tool_then_finish())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 50, "stream": True,
                    "thinking": {"type": "disabled"},
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [{"name": "get_weather", "description": "d",
                               "input_schema": {"type": "object"}}]})
                return r
        r = self._run(go())
        # parse indices of content_block_start/stop to ensure they're consistent
        starts = []
        for l in r.text.splitlines():
            if l.startswith("data: ") and "content_block_start" in l:
                d = json.loads(l[6:])
                starts.append((d["index"], d["content_block"]["type"]))
        self.assertTrue(len(starts) >= 1)
        # tool_use block must exist
        self.assertTrue(any(t == "tool_use" for _, t in starts))

    def test_error_passthrough(self):
        self._set_client(lambda req: httpx.Response(400, content=b'{"error":{"message":"bad"}}', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 10, "messages": []})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400)
        d = r.json()
        self.assertEqual(d["type"], "error")
        self.assertIn("error", d)

    def test_count_tokens(self):
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages/count_tokens", json={
                    "model": "claude-3",
                    "messages": [{"role": "user", "content": "hello world"}]})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("input_tokens", d)
        self.assertGreater(d["input_tokens"], 0)

    def test_system_param_passed_upstream(self):
        seen = {}
        def responder(req):
            body = json.loads(req.content)
            seen["prompt"] = body.get("prompt")
            return _sse_text_then_finish(["ok"])
        self._set_client(responder)
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", json={
                    "model": "claude-3", "max_tokens": 10,
                    "system": "be brief",
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        self._run(go())
        self.assertEqual(seen["prompt"][0]["role"], "system")


class TestErrorMapping(_Base):
    def test_invalid_json_chat(self):
        """Non-JSON body must return 400, not 500."""
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", content=b"not json",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400, f"got {r.status_code}: {r.text[:200]}")

    def test_invalid_json_responses(self):
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/responses", content=b"not json",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400, f"got {r.status_code}: {r.text[:200]}")

    def test_invalid_json_messages(self):
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/messages", content=b"not json",
                                 headers={"Content-Type": "application/json", "x-api-key": "dummy"})
                return r
        r = self._run(go())
        self.assertEqual(r.status_code, 400, f"got {r.status_code}: {r.text[:200]}")

    def test_chat_error_shape(self):
        self._set_client(lambda req: httpx.Response(500, content=b'{"error":{"message":"boom"}}', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        # all-500 should exhaust retries then return 500
        self.assertEqual(r.status_code, 500)
        d = r.json()
        self.assertIn("error", d)


class TestStreamingEdgeCases(_Base):
    def test_upstream_abrupt_end_stream(self):
        """Upstream closes mid-stream without finish: must emit graceful stop."""
        self._set_client(lambda req: httpx.Response(200, content=b'data: {"type":"text-delta","delta":"hi"}\n\n', request=req))
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        self.assertTrue(r.text.rstrip().endswith("DONE]"), f"no: {r.text[-200:]}")
        self.assertIn('"finish_reason": "stop"', r.text)

    def test_empty_delta_stream(self):
        """Stream with zero content deltas still terminates cleanly."""
        self._set_client(lambda req: _sse_ok())
        async def go():
            async with self._app_client() as c:
                r = await c.post("/v1/chat/completions", json={
                    "model": "zai/glm-5.2", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
                return r
        r = self._run(go())
        self.assertIn("data", r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
