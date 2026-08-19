#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.115.0",
#     "uvicorn[standard]>=0.30.0",
#     "httpx>=0.27.0",
# ]
# ///

import os
import sys
import time
import json
import uuid
import random
import logging
import asyncio
import argparse
from pathlib import Path
from collections import deque
from typing import Any, AsyncGenerator, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

__version__ = "0.2.0"

logger = logging.getLogger("fx-gateway-proxy")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://ai-gateway.vercel.sh/v3/ai/language-model")
USER_AGENT = os.environ.get("FX_USER_AGENT", "fx/0.0.3")
DEFAULT_KEY_PATH = Path.home() / ".fx" / "api-key"
DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "18080"))

PLACEHOLDER_KEYS = ("dummy", "none", "null", "placeholder", "ollama")

# Adaptive key routing: sliding-window stats, learned limits, exponential-backoff cooldown
MAX_KEY_RETRIES = int(os.environ.get("FX_MAX_KEY_RETRIES", "3"))
KEY_COOLDOWN_BASE = float(os.environ.get("FX_COOLDOWN_BASE", "30"))    # first 429 cooldown (s)
KEY_COOLDOWN_MAX = float(os.environ.get("FX_COOLDOWN_MAX", "300"))     # backoff cap (s)
EST_RPM_LIMIT = float(os.environ.get("FX_LIMIT_RPM", "60"))            # initial per-key RPM estimate
EST_TPM_LIMIT = float(os.environ.get("FX_LIMIT_TPM", "20000"))         # initial per-key TPM estimate
EST_RPM_MAX = float(os.environ.get("FX_LIMIT_RPM_MAX", "600"))         # learned ceiling cap
EST_TPM_MAX = float(os.environ.get("FX_LIMIT_TPM_MAX", "1000000"))     # learned ceiling cap

MODELS = (
    {"id": "zai/glm-5.2", "object": "model", "owned_by": "zai", "permission": []},
    {"id": "zai/glm-5.2-fast", "object": "model", "owned_by": "zai", "permission": []},
)


def resolve_keys(explicit_key: str = "") -> List[str]:
    """Resolve API keys from explicit param, env (comma/newline separated multi-key), or ~/.fx/api-key file."""
    if explicit_key and explicit_key.lower() not in PLACEHOLDER_KEYS:
        return [explicit_key.strip()]

    def split_keys(raw: str) -> List[str]:
        return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

    keys: List[str] = []
    for env_name in ("AI_GATEWAY_API_KEYS", "AI_GATEWAY_API_KEY"):
        raw = os.environ.get(env_name, "")
        if raw.strip():
            keys.extend(split_keys(raw))
    if not keys and DEFAULT_KEY_PATH.exists():
        try:
            keys.extend(split_keys(DEFAULT_KEY_PATH.read_text()))
        except Exception:
            pass
    return keys


def mask_key(key: str) -> str:
    """Partial key for logs and stats: abcd...wxyz"""
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:7] + "..." + key[-4:]


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


# --------------------------------------------------------------------------- #
# OpenAI <-> Vercel AI SDK v3 converters
# --------------------------------------------------------------------------- #

def convert_messages_to_v3(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI messages structure to Vercel AI SDK v3 Language Model prompt format."""
    prompt = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role in ("system", "developer"):
            prompt.append({
                "role": "system",
                "content": content if isinstance(content, str) else str(content or "")
            })
        elif role == "user":
            parts = []
            if isinstance(content, str):
                # Ensure non-empty string to avoid Vercel AI SDK 400 validation error
                parts.append({"type": "text", "text": content if content.strip() else " "})
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            txt = item.get("text", "")
                            parts.append({"type": "text", "text": txt if txt.strip() else " "})
                        elif item.get("type") == "image_url":
                            parts.append({"type": "image", "image": item.get("image_url", {}).get("url", "")})
                    else:
                        parts.append({"type": "text", "text": str(item) if str(item).strip() else " "})
            else:
                parts.append({"type": "text", "text": str(content) if content and str(content).strip() else " "})
            prompt.append({"role": "user", "content": parts})
        elif role == "assistant":
            parts = []
            if content:
                parts.append({"type": "text", "text": content if isinstance(content, str) else str(content)})
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", {})
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except Exception:
                        args = args_raw if isinstance(args_raw, dict) else {}
                    parts.append({
                        "type": "tool-call",
                        "toolCallId": tc.get("id", str(uuid.uuid4())),
                        "toolName": fn.get("name", ""),
                        "input": args if isinstance(args, dict) else {}
                    })
            if not parts:
                parts.append({"type": "text", "text": " "})
            prompt.append({"role": "assistant", "content": parts})
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_name = msg.get("name", "tool")
            out_str = content if isinstance(content, str) else json.dumps(content or "")
            prompt.append({
                "role": "tool",
                "content": [{
                    "type": "tool-result",
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "output": {"type": "text", "value": out_str or "{}"}
                }]
            })
    return prompt


def convert_tools_to_v3(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Convert OpenAI tools format to Vercel AI SDK v3 function tools."""
    if not tools:
        return None
    v3_tools = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            v3_tools.append({
                "type": "function",
                "name": fn.get("name", t.get("name", "")),
                "description": fn.get("description", t.get("description", "")),
                "inputSchema": fn.get("parameters", t.get("inputSchema", {}))
            })
        else:
            v3_tools.append(t)
    return v3_tools


def convert_tool_choice_to_v3(tc: Any) -> Optional[Dict[str, Any]]:
    """Convert OpenAI tool_choice parameter to Vercel AI SDK v3 toolChoice format."""
    if not tc:
        return {"type": "auto"}
    if isinstance(tc, str):
        if tc in ("auto", "none", "required"):
            return {"type": tc}
        return {"type": "tool", "toolName": tc}
    if isinstance(tc, dict):
        if tc.get("type") == "function" and "function" in tc:
            return {"type": "tool", "toolName": tc["function"].get("name", "")}
        if "type" in tc:
            return tc
    return {"type": "auto"}


def map_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """Map client thinking/reasoning effort level to Vercel format."""
    if not effort:
        return None
    effort_lower = str(effort).lower()
    if effort_lower in ("xhigh", "max"):
        return "xhigh"
    if effort_lower == "high":
        return "high"
    if effort_lower in ("medium", "low", "minimal", "auto", "default"):
        return "auto"
    if effort_lower == "off":
        return "none"
    return effort


def extract_usage(event: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize Vercel usage payload into OpenAI usage object."""
    usage_obj = event.get("usage") or {}
    raw_u = usage_obj.get("raw") or {}
    in_tokens = usage_obj.get("inputTokens") or {}
    out_tokens = usage_obj.get("outputTokens") or {}
    pt_details = raw_u.get("prompt_tokens_details") or {}

    prompt_tokens = raw_u.get("prompt_tokens") or in_tokens.get("total", 0)
    completion_tokens = raw_u.get("completion_tokens") or out_tokens.get("total", 0)
    cached_tokens = pt_details.get("cached_tokens", 0) or in_tokens.get("cacheRead", 0)
    reasoning_tokens = raw_u.get("reasoning_tokens") or out_tokens.get("reasoning", 0)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {
            "cached_tokens": cached_tokens
        },
        "completion_tokens_details": {
            "reasoning_tokens": reasoning_tokens
        }
    }


# --------------------------------------------------------------------------- #
# FastAPI application
# --------------------------------------------------------------------------- #

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def _response_scope(response):
    """Async scope that closes a streamed httpx.Response on exit."""
    try:
        yield response
    finally:
        await response.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0),
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=200, keepalive_expiry=60.0),
    )
    logger.info("Shared HTTP connection pool initialized.")
    yield
    if http_client:
        await http_client.aclose()
        logger.info("Shared HTTP connection pool closed.")


app = FastAPI(
    title="FX Gateway Proxy",
    description="OpenAI-compatible reverse proxy for Vercel AI Gateway FX Free Promo Pool with Adaptive Multi-Key Routing",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@app.get("/")
async def health():
    return {"status": "ok", "service": "fx-gateway-proxy", "version": __version__}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": list(MODELS)}


@app.get("/v1/stats")
async def key_stats():
    """Live per-key routing statistics (masked keys)."""
    pool = get_key_pool()
    return {"keys": pool.stats(), "total": len(pool.keys)}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    body = await request.json()
    model = body.get("model", "zai/glm-5.2")
    stream = body.get("stream", False)
    messages = body.get("messages", [])
    tools = body.get("tools")
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens") or 128000

    # Reasoning effort mapping
    reasoning_raw = body.get("reasoning_effort") or body.get("thinking_level") or "xhigh"
    reasoning_mapped = map_reasoning_effort(reasoning_raw)

    # Resolve API Keys (multi-key pool with adaptive rotation)
    incoming_key = ""
    if authorization and authorization.startswith("Bearer "):
        incoming_key = authorization[7:].strip()
    key_pool = get_key_pool(incoming_key)

    if not key_pool.keys:
        raise HTTPException(
            status_code=401,
            detail="Missing API Key. Provide via Authorization header, AI_GATEWAY_API_KEYS / AI_GATEWAY_API_KEY env, or ~/.fx/api-key file."
        )

    # Session affinity handling for KV caching
    headers_in = request.headers
    session_id = (
        headers_in.get("x-session-id")
        or headers_in.get("x-session-affinity")
        or headers_in.get("session_id")
        or headers_in.get("x-client-request-id")
        or f"pi-{uuid.uuid4().hex[:16]}"
    )

    # Build Vercel v3 payload with full parameter passthrough
    prompt = convert_messages_to_v3(messages)
    v3_tools = convert_tools_to_v3(tools)

    v3_payload: Dict[str, Any] = {
        "prompt": prompt,
        "maxOutputTokens": max_tokens,
        "headers": {"user-agent": USER_AGENT}
    }

    # Transparent sampling parameter mappings
    if "temperature" in body:
        v3_payload["temperature"] = float(body["temperature"])
    if "top_p" in body:
        v3_payload["topP"] = float(body["top_p"])
    if "top_k" in body:
        v3_payload["topK"] = int(body["top_k"])
    if "presence_penalty" in body:
        v3_payload["presencePenalty"] = float(body["presence_penalty"])
    if "frequency_penalty" in body:
        v3_payload["frequencyPenalty"] = float(body["frequency_penalty"])
    if "seed" in body:
        v3_payload["seed"] = int(body["seed"])
    if "stop" in body:
        stop_val = body["stop"]
        v3_payload["stopSequences"] = [stop_val] if isinstance(stop_val, str) else list(stop_val)
    if "response_format" in body:
        rf = body["response_format"]
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            v3_payload["responseFormat"] = {"type": "json"}

    if v3_tools:
        v3_payload["tools"] = v3_tools
        v3_payload["toolChoice"] = convert_tool_choice_to_v3(body.get("tool_choice"))
    if reasoning_mapped and reasoning_mapped != "none":
        v3_payload["reasoning"] = reasoning_mapped

    def build_headers(api_key: str) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "HTTP-Referer": "https://github.com/vercel-labs/fx",
            "X-Title": "fx",
            "ai-gateway-protocol-version": "0.0.1",
            "ai-language-model-specification-version": "4",
            "ai-language-model-id": model,
            "ai-language-model-streaming": "true",
            "x-session-id": session_id,
            "x-session-affinity": session_id,
        }
        for th in ("traceparent", "tracestate", "x-request-id", "x-b3-traceid"):
            if th in headers_in:
                headers[th] = headers_in[th]
        return headers

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    client = http_client or httpx.AsyncClient(timeout=300.0)

    async def stream_generator() -> AsyncGenerator[str, None]:
        try:
            # Open upstream connection with multi-key adaptive rotation & retry
            attempts = min(MAX_KEY_RETRIES + 1, max(1, len(key_pool.keys)))
            response = None
            key = ""
            for attempt in range(attempts):
                key = key_pool.next()
                t_start = time.time()
                req = client.build_request("POST", UPSTREAM_URL, headers=build_headers(key), json=v3_payload)
                response = await client.send(req, stream=True)
                
                if response.status_code in (429, 503) and attempt + 1 < attempts:
                    if response.status_code == 429:
                        key_pool.mark_failed(key)
                        logger.warning(f"fx: 429 rate-limited on key={mask_key(key)}; rotating to next key")
                    else:
                        key_pool.mark_error(key)
                        logger.warning(f"fx: 503 error on key={mask_key(key)}; retrying next key")
                    await response.aclose()
                    continue
                break

            async with _response_scope(response):
                if response.status_code != 200:
                    err_bytes = await response.aread()
                    err_msg = err_bytes.decode(errors="replace")
                    logger.error(f"Gateway error ({response.status_code}): {err_msg}")
                    if response.status_code == 429:
                        key_pool.mark_failed(key)
                    else:
                        key_pool.mark_error(key)
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": f"\n[Gateway Error {response.status_code}]: {err_msg}"},
                            "finish_reason": "error"
                        }]
                    }
                    yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                    return

                tool_call_indices: Dict[str, int] = {}
                current_tool_idx = 0

                async for line in response.aiter_lines():
                    if await request.is_disconnected():
                        logger.info("Client disconnected early, terminating stream.")
                        break

                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue

                    raw_data = line[6:].strip()
                    if raw_data in ("[DONE]", "DONE"):
                        yield "data: [DONE]\n\n"
                        break

                    try:
                        event = json.loads(raw_data)
                    except Exception:
                        continue

                    ev_type = event.get("type")

                    if ev_type == "text-delta":
                        delta_text = event.get("delta", "")
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": delta_text},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    elif ev_type == "reasoning-delta":
                        delta_text = event.get("delta", "")
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"reasoning_content": delta_text},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    elif ev_type == "tool-input-start":
                        tc_id = event.get("id", str(uuid.uuid4()))
                        tool_name = event.get("toolName", "")
                        tool_call_indices[tc_id] = current_tool_idx
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": current_tool_idx,
                                        "id": tc_id,
                                        "type": "function",
                                        "function": {"name": tool_name, "arguments": ""}
                                    }]
                                },
                                "finish_reason": None
                            }]
                        }
                        current_tool_idx += 1
                        yield f"data: {json.dumps(chunk)}\n\n"

                    elif ev_type == "tool-input-delta":
                        tc_id = event.get("id")
                        idx = tool_call_indices.get(tc_id, 0)
                        delta_args = event.get("delta", "")
                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{
                                        "index": idx,
                                        "function": {"arguments": delta_args}
                                    }]
                                },
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

                    elif ev_type == "finish":
                        finish_reason_obj = event.get("finishReason", {})
                        raw_reason = finish_reason_obj.get("unified") or finish_reason_obj.get("raw") if isinstance(finish_reason_obj, dict) else str(finish_reason_obj or "stop")
                        finish_reason = "stop"
                        if raw_reason in ("tool-calls", "tool_calls"):
                            finish_reason = "tool_calls"
                        elif raw_reason == "length":
                            finish_reason = "length"

                        usage_dict = extract_usage(event)
                        key_pool.mark_success(key, usage_dict["total_tokens"], time.time() - t_start)

                        chunk = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason
                            }],
                            "usage": usage_dict
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.info("Stream task cancelled by client.")
            raise

    if stream:
        return StreamingResponse(stream_generator(), media_type="text/event-stream")

    # Non-streaming implementation
    full_content: List[str] = []
    full_reasoning: List[str] = []
    tool_calls_map: Dict[str, Dict[str, Any]] = {}
    usage_info: Dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0}
    }

    attempts = min(MAX_KEY_RETRIES + 1, max(1, len(key_pool.keys)))
    response = None
    key = ""
    for attempt in range(attempts):
        key = key_pool.next()
        t_start = time.time()
        req = client.build_request("POST", UPSTREAM_URL, headers=build_headers(key), json=v3_payload)
        response = await client.send(req, stream=True)
        if response.status_code in (429, 503) and attempt + 1 < attempts:
            if response.status_code == 429:
                key_pool.mark_failed(key)
                logger.warning(f"fx: 429 rate-limited on key={mask_key(key)}; rotating to next key")
            else:
                key_pool.mark_error(key)
                logger.warning(f"fx: 503 error on key={mask_key(key)}; retrying next key")
            await response.aclose()
            continue
        break

    async with _response_scope(response):
        if response.status_code != 200:
            err_bytes = await response.aread()
            err_str = err_bytes.decode(errors="replace")
            if response.status_code == 429:
                key_pool.mark_failed(key)
            else:
                key_pool.mark_error(key)
            try:
                err_json = json.loads(err_str)
                err_msg = err_json.get("error", {}).get("message") or err_str
            except Exception:
                err_msg = err_str
            return JSONResponse(
                status_code=response.status_code,
                content={
                    "error": {
                        "message": err_msg,
                        "type": "upstream_error",
                        "code": response.status_code
                    }
                }
            )

        finish_reason = "stop"
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            raw_data = line[6:].strip()
            if raw_data in ("[DONE]", "DONE"):
                break
            try:
                event = json.loads(raw_data)
            except Exception:
                continue

            ev_type = event.get("type")
            if ev_type == "text-delta":
                full_content.append(event.get("delta", ""))
            elif ev_type == "reasoning-delta":
                full_reasoning.append(event.get("delta", ""))
            elif ev_type == "tool-input-start":
                tc_id = event.get("id", str(uuid.uuid4()))
                tool_calls_map[tc_id] = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": event.get("toolName", ""), "arguments": ""}
                }
            elif ev_type == "tool-input-delta":
                tc_id = event.get("id")
                if tc_id in tool_calls_map:
                    tool_calls_map[tc_id]["function"]["arguments"] += event.get("delta", "")
            elif ev_type == "tool-call":
                tc_id = event.get("toolCallId")
                inp = event.get("input")
                args_str = inp if isinstance(inp, str) else json.dumps(inp or {})
                tool_calls_map[tc_id] = {
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": event.get("toolName", ""), "arguments": args_str}
                }
            elif ev_type == "finish":
                fr_obj = event.get("finishReason", {})
                fr = fr_obj.get("unified") or fr_obj.get("raw") if isinstance(fr_obj, dict) else str(fr_obj or "stop")
                if fr in ("tool-calls", "tool_calls"):
                    finish_reason = "tool_calls"
                elif fr == "length":
                    finish_reason = "length"

                usage_info = extract_usage(event)

    content_str = "".join(full_content)
    res_message: Dict[str, Any] = {
        "role": "assistant",
        "content": content_str if (content_str or not tool_calls_map) else None
    }
    if full_reasoning:
        res_message["reasoning_content"] = "".join(full_reasoning)
    if tool_calls_map:
        res_message["tool_calls"] = list(tool_calls_map.values())

    key_pool.mark_success(key, usage_info.get("total_tokens", 0), time.time() - t_start)

    return {
        "id": req_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": model,
        "choices": [{
            "index": 0,
            "message": res_message,
            "finish_reason": finish_reason
        }],
        "usage": usage_info
    }


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fx-gateway-proxy",
        description="FX Gateway Proxy: OpenAI-compatible reverse proxy for Vercel AI Gateway FX promotional free pool with Adaptive Multi-Key Routing",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--api-key", default=None, help="Vercel AI Gateway API key (overrides AI_GATEWAY_API_KEY / ~/.fx/api-key)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"], help="Logging level (default: info)")
    parser.add_argument("--version", action="version", version=f"fx-gateway-proxy {__version__}")

    args = parser.parse_args()

    if args.api_key:
        os.environ["AI_GATEWAY_API_KEY"] = args.api_key.strip()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info(f"Starting FX Gateway Proxy on http://{args.host}:{args.port} ...")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
