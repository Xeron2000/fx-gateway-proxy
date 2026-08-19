import time
import json
import uuid
import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, Response

from .config import (
    UPSTREAM_URL,
    USER_AGENT,
    MODELS,
    MAX_KEY_RETRIES,
    __version__,
    mask_key,
)
from .pool import get_key_pool
from .converter import (
    convert_messages_to_v3,
    convert_tools_to_v3,
    convert_tool_choice_to_v3,
    map_reasoning_effort,
    extract_usage,
    responses_input_to_messages,
    anthropic_messages_to_chat,
    anthropic_tools_to_chat,
    anthropic_tool_choice_to_chat,
)

logger = logging.getLogger("fx-gateway-proxy")

http_client: Optional[httpx.AsyncClient] = None
BASE_DELAY = float(os.environ.get("FX_BASE_DELAY", "0.8"))
MAX_DELAY = float(os.environ.get("FX_MAX_DELAY", "20.0"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff: min(BASE_DELAY * 2^attempt, MAX_DELAY)."""
    return min(BASE_DELAY * (2 ** attempt), MAX_DELAY)


def _extract_auth(request: Request, authorization: Optional[str]) -> str:
    """Unified key extraction: Bearer, x-api-key, X-Api-Key."""
    if authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        if auth:
            return auth
    # check x-api-key variants (headers are case-insensitive)
    for hdr in ("x-api-key", "X-Api-Key", "X-API-KEY", "x_api_key"):
        val = request.headers.get(hdr)
        if val:
            v = val.strip()
            if v.lower().startswith("bearer "):
                v = v[7:].strip()
            if v:
                return v
    # fallback: raw Authorization header (case-insensitive lookup)
    alt = request.headers.get("authorization")
    if alt:
        a = alt.strip()
        if a.lower().startswith("bearer "):
            a = a[7:].strip()
        if a:
            return a
    return ""


# ── Responses helpers (pure) ──

def _responses_tools_to_chat(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools
    out = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or "function" in tool:
            out.append(tool)
            continue
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or tool.get("input_schema") or {},
            },
        })
    return out


def _responses_to_chat_body(body: Dict[str, Any]) -> Dict[str, Any]:
    chat_body = dict(body)
    chat_body["model"] = _map_model(body.get("model", "zai/glm-5.2"))
    chat_body["messages"] = responses_input_to_messages(body.get("input", ""), body.get("instructions"))
    chat_body["stream"] = bool(body.get("stream", False))
    if "max_output_tokens" in body and "max_completion_tokens" not in body:
        chat_body["max_completion_tokens"] = body["max_output_tokens"]
    if "tools" in body:
        chat_body["tools"] = _responses_tools_to_chat(body["tools"])
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function" and "function" not in tc:
        chat_body["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat_body["reasoning_effort"] = reasoning["effort"]
    text_format = body.get("text", {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(text_format, dict) and text_format.get("type") in ("json_object", "json_schema"):
        chat_body["response_format"] = {"type": "json_object"}
    return chat_body


def _response_event(event_type: str, payload: Dict[str, Any]) -> str:
    event = {"type": event_type, **payload}
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _response_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    pd = usage.get("prompt_tokens_details") or {}
    cd = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": pt,
        "output_tokens": ct,
        "total_tokens": usage.get("total_tokens", pt + ct),
        "input_tokens_details": {"cached_tokens": pd.get("cached_tokens", 0) or 0},
        "output_tokens_details": {"reasoning_tokens": cd.get("reasoning_tokens", 0) or 0},
    }


def _response_metadata(response_id: str, model: str, created_at: int, status: str) -> Dict[str, Any]:
    return {"id": response_id, "object": "response", "created_at": created_at, "status": status, "error": None, "incomplete_details": None, "model": model, "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}


def _chat_to_response(chat_result: Dict[str, Any], request_body: Dict[str, Any]) -> Dict[str, Any]:
    choice = (chat_result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    model = request_body.get("model") or chat_result.get("model") or "zai/glm-5.2"
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = chat_result.get("created", int(time.time()))
    output: List[Dict[str, Any]] = []
    reasoning = message.get("reasoning_content") or ""
    if reasoning:
        output.append({"id": f"rs_{uuid.uuid4().hex}", "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": reasoning}]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"id": tc.get("id") or f"fc_{uuid.uuid4().hex}", "type": "function_call", "status": "completed", "call_id": tc.get("id") or f"call_{uuid.uuid4().hex}", "name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")})
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    if content or not output:
        output.append({"id": f"msg_{uuid.uuid4().hex}", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": content, "annotations": []}]})
    status = "incomplete" if choice.get("finish_reason") == "length" else "completed"
    result = _response_metadata(response_id, model, created_at, status)
    result.update({"output": output, "output_text": content, "usage": _response_usage(chat_result.get("usage") or {}), "instructions": request_body.get("instructions"), "metadata": request_body.get("metadata") or {}, "store": bool(request_body.get("store", False)), "truncation": request_body.get("truncation", "disabled")})
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


async def _chat_stream_to_response_stream(chat_response: StreamingResponse, request_body: Dict[str, Any]) -> AsyncGenerator[str, None]:
    model = request_body.get("model", "zai/glm-5.2")
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    message_id = f"msg_{uuid.uuid4().hex}"
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    function_calls: Dict[int, Dict[str, Any]] = {}
    message_output_index: Optional[int] = None
    usage: Dict[str, Any] = {}
    finish_reason = "stop"
    yield _response_event("response.created", {"response": _response_metadata(response_id, model, created_at, "in_progress")})
    async for raw_chunk in chat_response.body_iterator:
        if isinstance(raw_chunk, bytes):
            raw_chunk = raw_chunk.decode("utf-8", errors="replace")
        for line in str(raw_chunk).splitlines():
            if not line.startswith("data: "):
                continue
            raw_data = line[6:].strip()
            if raw_data in ("[DONE]", "DONE"):
                continue
            try:
                chunk = json.loads(raw_data)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            delta = choice.get("delta") or {}
            if delta.get("content"):
                if message_output_index is None:
                    message_output_index = len(function_calls)
                    yield _response_event("response.output_item.added", {"output_index": message_output_index, "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                    yield _response_event("response.content_part.added", {"item_id": message_id, "output_index": message_output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
                text = delta["content"]
                text_parts.append(text)
                yield _response_event("response.output_text.delta", {"item_id": message_id, "output_index": message_output_index, "content_index": 0, "delta": text})
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tool_delta in delta.get("tool_calls") or []:
                idx = int(tool_delta.get("index", 0))
                function = tool_delta.get("function") or {}
                state = function_calls.get(idx)
                if state is None:
                    call_id = tool_delta.get("id") or f"call_{uuid.uuid4().hex}"
                    state = {"id": tool_delta.get("id") or f"fc_{uuid.uuid4().hex}", "call_id": call_id, "name": function.get("name", ""), "arguments": "", "output_index": idx}
                    function_calls[idx] = state
                    yield _response_event("response.output_item.added", {"output_index": state["output_index"], "item": {"id": state["id"], "type": "function_call", "status": "in_progress", "call_id": state["call_id"], "name": state["name"], "arguments": ""}})
                args = function.get("arguments", "")
                if args:
                    state["arguments"] += args
                    yield _response_event("response.function_call_arguments.delta", {"item_id": state["id"], "output_index": state["output_index"], "delta": args})
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
    output: List[Dict[str, Any]] = []
    if reasoning_parts:
        output.append({"id": f"rs_{uuid.uuid4().hex}", "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": "".join(reasoning_parts)}]})
    for idx in sorted(function_calls):
        state = function_calls[idx]
        output.append({"id": state["id"], "type": "function_call", "status": "completed", "call_id": state["call_id"], "name": state["name"], "arguments": state["arguments"]})
        yield _response_event("response.function_call_arguments.done", {"item_id": state["id"], "output_index": state["output_index"], "arguments": state["arguments"]})
        yield _response_event("response.output_item.done", {"output_index": state["output_index"], "item": output[-1]})
    if message_output_index is not None:
        message_item = {"id": message_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}]}
        output.append(message_item)
        yield _response_event("response.output_text.done", {"item_id": message_id, "output_index": message_output_index, "content_index": 0, "text": "".join(text_parts)})
        yield _response_event("response.content_part.done", {"item_id": message_id, "output_index": message_output_index, "content_index": 0, "part": message_item["content"][0]})
        yield _response_event("response.output_item.done", {"output_index": message_output_index, "item": message_item})
    output.sort(key=lambda item: item.get("id", ""))
    status = "incomplete" if finish_reason == "length" else "completed"
    final = _response_metadata(response_id, model, created_at, status)
    final.update({"output": output, "output_text": "".join(text_parts), "usage": _response_usage(usage), "instructions": request_body.get("instructions"), "metadata": request_body.get("metadata") or {}, "store": bool(request_body.get("store", False)), "truncation": request_body.get("truncation", "disabled")})
    if status == "incomplete":
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    yield _response_event("response.completed", {"response": final})


SUPPORTED_MODELS = {m["id"] for m in MODELS}

def _map_model(model: str) -> str:
    if not model or model in SUPPORTED_MODELS:
        return model or "zai/glm-5.2"
    # anthropic / openai model aliases -> map to zai pool
    low = model.lower()
    if "fast" in low or "haiku" in low:
        return "zai/glm-5.2-fast"
    return "zai/glm-5.2"

# ── Anthropic helpers (pure) ──

def _anthropic_to_chat_body(body: Dict[str, Any]) -> Dict[str, Any]:
    chat_body: Dict[str, Any] = {}
    raw_model = body.get("model", "zai/glm-5.2")
    chat_body["model"] = _map_model(raw_model)
    chat_body["messages"] = anthropic_messages_to_chat(body.get("messages", []), body.get("system"))
    # max_tokens is required in Anthropic, map to max_completion_tokens
    if "max_tokens" in body:
        chat_body["max_completion_tokens"] = body["max_tokens"]
    chat_body["stream"] = bool(body.get("stream", False))
    for k in ("temperature", "top_p", "top_k"):
        if k in body:
            chat_body[k] = body[k]
    if "stop_sequences" in body:
        chat_body["stop"] = body["stop_sequences"]
    tools = anthropic_tools_to_chat(body.get("tools"))
    if tools:
        chat_body["tools"] = tools
    tc = anthropic_tool_choice_to_chat(body.get("tool_choice"))
    if tc:
        chat_body["tool_choice"] = tc
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        budget = thinking.get("budget_tokens", 0)
        # map budget to effort
        if isinstance(budget, int) and budget > 16000:
            chat_body["reasoning_effort"] = "xhigh"
        elif isinstance(budget, int) and budget > 8000:
            chat_body["reasoning_effort"] = "high"
        else:
            chat_body["reasoning_effort"] = "medium"
    elif thinking and thinking.get("type") == "disabled":
        chat_body["reasoning_effort"] = "off"
    else:
        # anthropic default is no thinking -> lighter reasoning to avoid token blow-up
        chat_body["reasoning_effort"] = "auto"
    return chat_body


def _anthropic_usage(chat_usage: Dict[str, Any]) -> Dict[str, Any]:
    pt = chat_usage.get("prompt_tokens", 0) or 0
    ct = chat_usage.get("completion_tokens", 0) or 0
    pd = chat_usage.get("prompt_tokens_details") or {}
    cached = pd.get("cached_tokens", 0) or 0
    return {"input_tokens": pt, "output_tokens": ct, "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached}


def _chat_to_anthropic(chat_result: Dict[str, Any], request_body: Dict[str, Any]) -> Dict[str, Any]:
    choice = (chat_result.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    model = request_body.get("model") or chat_result.get("model") or "zai/glm-5.2"
    content: List[Dict[str, Any]] = []
    reasoning = msg.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    text = msg.get("content")
    if text:
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {"value": args}
        content.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"), "name": fn.get("name", ""), "input": args})
    if not content:
        content.append({"type": "text", "text": ""})
    finish = choice.get("finish_reason", "stop")
    stop_reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "tool_use": "tool_use", "stop_sequence": "stop_sequence"}.get(finish, "end_turn")
    usage = _anthropic_usage(chat_result.get("usage") or {})
    return {"id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "content": content, "model": model, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}


async def _chat_stream_to_anthropic_stream(chat_response: StreamingResponse, request_body: Dict[str, Any]) -> AsyncGenerator[str, None]:
    model = request_body.get("model", "zai/glm-5.2")
    msg_id = f"msg_{uuid.uuid4().hex}"
    # state: track indices
    # Anthropic streaming: message_start -> content_block_start/delta/stop -> message_delta -> message_stop
    usage: Dict[str, Any] = {}
    finish_reason = "stop"
    # buffers
    text_buf = ""
    thinking_buf = ""
    tool_calls: Dict[int, Dict[str, Any]] = {}
    # index assignment: 0 = thinking if exists, then tool_use blocks, then text
    # We need to know final indices ahead, but we can assign dynamically:
    # We'll emit blocks as they appear: thinking idx 0, tools idx 1..n, text idx last
    # To keep indices stable, we defer text block start until we know if reasoning exists?
    # Simpler: emit in order of appearance: thinking first if any reasoning delta before text, else text first.
    text_index: Optional[int] = None
    thinking_index: Optional[int] = None
    thinking_started = False
    text_started = False
    # For initial message_start, usage input is unknown until upstream finishes, use 0
    yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"
    async for raw_chunk in chat_response.body_iterator:
        if isinstance(raw_chunk, bytes):
            raw_chunk = raw_chunk.decode("utf-8", errors="replace")
        for line in str(raw_chunk).splitlines():
            if not line.startswith("data: "):
                continue
            raw_data = line[6:].strip()
            if raw_data in ("[DONE]", "DONE"):
                continue
            try:
                chunk = json.loads(raw_data)
            except Exception:
                continue
            choices = chunk.get("choices") or []
            choice = choices[0] if choices else {}
            delta = choice.get("delta") or {}
            # thinking -> map to thinking block
            if delta.get("reasoning_content"):
                if not thinking_started:
                    thinking_started = True
                    thinking_index = 0
                    # shift text_index if already started? if text started at 0, need to move it
                    # but we avoid by ensuring thinking always idx 0, so text should be idx 1+tools
                    # if text already started, we already emitted text at 0, now thinking would be out-of-order.
                    # For simplicity, if text_started before thinking, we keep thinking as next index.
                    if text_started:
                        thinking_index = (max(tool_calls.keys()) + 1) if tool_calls else 1
                        # text already at 0, thinking at 1
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': thinking_index, 'content_block': {'type': 'thinking', 'thinking': ''}}, ensure_ascii=False)}\n\n"
                thinking_buf += delta["reasoning_content"]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': thinking_index, 'delta': {'type': 'thinking_delta', 'thinking': delta['reasoning_content']}}, ensure_ascii=False)}\n\n"
            if delta.get("content"):
                if not text_started:
                    text_started = True
                    # determine text index: after thinking + tools
                    if thinking_index is not None:
                        base = 1
                    else:
                        base = 0
                    # tools indices start after thinking, so text is after tools
                    # but tools may not have started yet; we allocate text as next after current tools
                    # If tools already exist, text index should be max(tools)+1
                    if tool_calls:
                        text_index = max(tool_calls.keys()) + 1
                        if thinking_index is not None and text_index <= thinking_index:
                            text_index = thinking_index + 1
                    else:
                        text_index = base
                        if thinking_index is not None and text_index == thinking_index:
                            text_index += 1
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': text_index, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
                text_buf += delta["content"]
                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': text_index, 'delta': {'type': 'text_delta', 'text': delta['content']}}, ensure_ascii=False)}\n\n"
            for tool_delta in delta.get("tool_calls") or []:
                idx = int(tool_delta.get("index", 0))
                # map chat tool index to anthropic index: offset by thinking
                anthro_idx = idx + (1 if thinking_index is not None else 0)
                # if text already started, its index may collide; but tool indices are before text, so if text_index exists and anthro_idx >= text_index, we need to push text further (rare)
                # for ponytail, assume tool deltas arrive before text deltas mostly, or interleave handled
                fn = tool_delta.get("function") or {}
                state = tool_calls.get(anthro_idx)
                if state is None:
                    call_id = tool_delta.get("id") or f"toolu_{uuid.uuid4().hex[:10]}"
                    state = {"id": call_id, "name": fn.get("name", ""), "input_json": "", "anthro_idx": anthro_idx}
                    tool_calls[anthro_idx] = state
                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': anthro_idx, 'content_block': {'type': 'tool_use', 'id': call_id, 'name': state['name'], 'input': {}}}, ensure_ascii=False)}\n\n"
                args = fn.get("arguments", "")
                if args:
                    state["input_json"] += args
                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': anthro_idx, 'delta': {'type': 'input_json_delta', 'partial_json': args}}, ensure_ascii=False)}\n\n"
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            if chunk.get("usage"):
                usage = chunk["usage"]
    # close blocks
    # thinking stop
    if thinking_started and thinking_index is not None:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': thinking_index}, ensure_ascii=False)}\n\n"
    # tool stops
    for idx in sorted(tool_calls):
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': idx}, ensure_ascii=False)}\n\n"
    if text_started and text_index is not None:
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': text_index}, ensure_ascii=False)}\n\n"
    anthro_stop = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "tool_use": "tool_use", "stop_sequence": "stop_sequence"}.get(finish_reason, "end_turn")
    anthro_usage = _anthropic_usage(usage) if usage else {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': anthro_stop, 'stop_sequence': None}, 'usage': {'output_tokens': anthro_usage.get('output_tokens', 0)}}, ensure_ascii=False)}\n\n"
    yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"


@asynccontextmanager
async def _response_scope(response):
    try:
        yield response
    finally:
        await response.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0), limits=httpx.Limits(max_keepalive_connections=50, max_connections=200, keepalive_expiry=60.0))
    logger.info("Shared HTTP connection pool initialized.")
    yield
    if http_client:
        await http_client.aclose()
        logger.info("Shared HTTP connection pool closed.")


def create_app() -> FastAPI:
    app = FastAPI(title="FX Gateway Proxy", description="OpenAI-compatible reverse proxy for Vercel AI Gateway FX Free Promo Pool with Adaptive Multi-Key Routing", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    @app.get("/")
    async def health():
        return {"status": "ok", "service": "fx-gateway-proxy", "version": __version__}

    @app.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": list(MODELS)}

    @app.get("/v1/stats")
    async def key_stats():
        pool = get_key_pool()
        return {"keys": pool.stats(), "total": len(pool.keys)}

    # ── shared upstream executor ──
    async def _proxy_chat(chat_body: Dict[str, Any], request: Request, auth_raw: str):
        """Execute chat_body against Vercel upstream with retries, returning StreamingResponse or dict."""
        model = chat_body.get("model", "zai/glm-5.2")
        stream = bool(chat_body.get("stream", False))
        messages = chat_body.get("messages", [])
        tools = chat_body.get("tools")
        max_tokens = chat_body.get("max_tokens") or chat_body.get("max_completion_tokens") or 128000
        reasoning_raw = chat_body.get("reasoning_effort") or chat_body.get("thinking_level") or "xhigh"
        reasoning_mapped = map_reasoning_effort(reasoning_raw)
        incoming_key = auth_raw or ""
        # placeholder check: if incoming_key is dummy, resolve_keys will fallback to env; so pass through
        key_pool = get_key_pool(incoming_key)
        if not key_pool.keys:
            raise HTTPException(status_code=401, detail="Missing API Key. Provide via Authorization header, x-api-key header, AI_GATEWAY_API_KEYS / AI_GATEWAY_API_KEY env, or ~/.fx/api-key file.")
        headers_in = request.headers
        session_id = headers_in.get("x-session-id") or headers_in.get("x-session-affinity") or headers_in.get("session_id") or headers_in.get("x-client-request-id") or f"pi-{uuid.uuid4().hex[:16]}"
        prompt = convert_messages_to_v3(messages)
        v3_tools = convert_tools_to_v3(tools)
        v3_payload: Dict[str, Any] = {"prompt": prompt, "maxOutputTokens": max_tokens, "headers": {"user-agent": USER_AGENT, "x-title": "fx"}}
        if "temperature" in chat_body:
            v3_payload["temperature"] = float(chat_body["temperature"])
        if "top_p" in chat_body:
            v3_payload["topP"] = float(chat_body["top_p"])
        if "top_k" in chat_body:
            v3_payload["topK"] = int(chat_body["top_k"])
        if "presence_penalty" in chat_body:
            v3_payload["presencePenalty"] = float(chat_body["presence_penalty"])
        if "frequency_penalty" in chat_body:
            v3_payload["frequencyPenalty"] = float(chat_body["frequency_penalty"])
        if "seed" in chat_body:
            v3_payload["seed"] = int(chat_body["seed"])
        if "stop" in chat_body:
            stop_val = chat_body["stop"]
            v3_payload["stopSequences"] = [stop_val] if isinstance(stop_val, str) else list(stop_val)
        if "response_format" in chat_body:
            rf = chat_body["response_format"]
            if isinstance(rf, dict) and rf.get("type") == "json_object":
                v3_payload["responseFormat"] = {"type": "json"}
        if v3_tools:
            v3_payload["tools"] = v3_tools
            v3_payload["toolChoice"] = convert_tool_choice_to_v3(chat_body.get("tool_choice"))
        if reasoning_mapped and reasoning_mapped != "none":
            v3_payload["reasoning"] = reasoning_mapped

        def build_headers(api_key: str) -> Dict[str, str]:
            h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": USER_AGENT, "HTTP-Referer": "https://github.com/vercel-labs/fx", "X-Title": "fx", "ai-gateway-protocol-version": "0.0.1", "ai-language-model-specification-version": "4", "ai-language-model-id": model, "ai-language-model-streaming": "true", "x-session-id": session_id, "x-session-affinity": session_id}
            for th in ("traceparent", "tracestate", "x-request-id", "x-b3-traceid"):
                if th in headers_in:
                    h[th] = headers_in[th]
            return h

        req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())
        client = http_client or httpx.AsyncClient(timeout=300.0)
        # ponytail: retry budget is FX_MAX_KEY_RETRIES+1 regardless of pool size
        attempts = MAX_KEY_RETRIES + 1

        async def stream_generator() -> AsyncGenerator[str, None]:
            try:
                response = None
                key = ""
                for attempt in range(attempts):
                    key = key_pool.next()
                    t_start = time.time()
                    try:
                        req = client.build_request("POST", UPSTREAM_URL, headers=build_headers(key), json=v3_payload)
                        response = await client.send(req, stream=True)
                    except Exception as e:
                        if attempt + 1 < attempts:
                            wait_time = _backoff_delay(attempt)
                            logger.warning(f"fx: network exception on key={mask_key(key)}: {e}; retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                            continue
                        raise
                    if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                        if response.status_code == 429:
                            key_pool.mark_failed(key)
                            logger.warning(f"fx: 429 rate-limited on key={mask_key(key)}; backing off {_backoff_delay(attempt)}s")
                        else:
                            key_pool.mark_error(key)
                            logger.warning(f"fx: {response.status_code} error on key={mask_key(key)}; retrying next key")
                        await response.aclose()
                        await asyncio.sleep(_backoff_delay(attempt))
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
                        chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {"content": f"\n[Gateway Error {response.status_code}]: {err_msg}"}, "finish_reason": "error"}]}
                        yield f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
                        return
                    tool_call_indices: Dict[str, int] = {}
                    current_tool_idx = 0
                    stream_finished = False
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
                            chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev_type == "reasoning-delta":
                            delta_text = event.get("delta", "")
                            chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {"reasoning_content": delta_text}, "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev_type == "tool-input-start":
                            tc_id = event.get("id", str(uuid.uuid4()))
                            tool_name = event.get("toolName", "")
                            tool_call_indices[tc_id] = current_tool_idx
                            chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": current_tool_idx, "id": tc_id, "type": "function", "function": {"name": tool_name, "arguments": ""}}]}, "finish_reason": None}]}
                            current_tool_idx += 1
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev_type == "tool-input-delta":
                            tc_id = event.get("id")
                            idx = tool_call_indices.get(tc_id, 0)
                            delta_args = event.get("delta", "")
                            chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": idx, "function": {"arguments": delta_args}}]}, "finish_reason": None}]}
                            yield f"data: {json.dumps(chunk)}\n\n"
                        elif ev_type == "finish":
                            fr_obj = event.get("finishReason", {})
                            raw_reason = fr_obj.get("unified") or fr_obj.get("raw") if isinstance(fr_obj, dict) else str(fr_obj or "stop")
                            finish_reason = "stop"
                            if raw_reason in ("tool-calls", "tool_calls"):
                                finish_reason = "tool_calls"
                            elif raw_reason == "length":
                                finish_reason = "length"
                            usage_dict = extract_usage(event)
                            key_pool.mark_success(key, usage_dict["total_tokens"], time.time() - t_start)
                            stream_finished = True
                            chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}], "usage": usage_dict}
                            yield f"data: {json.dumps(chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                    if not stream_finished:
                        logger.warning(f"Upstream stream ended abruptly without finish event on key={mask_key(key)}, appending graceful stop chunk.")
                        guard_chunk = {"id": req_id, "object": "chat.completion.chunk", "created": created_ts, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                        yield f"data: {json.dumps(guard_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                logger.info("Stream task cancelled by client.")
                raise

        if stream:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        # Non-streaming
        full_content: List[str] = []
        full_reasoning: List[str] = []
        tool_calls_map: Dict[str, Dict[str, Any]] = {}
        usage_info: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "prompt_tokens_details": {"cached_tokens": 0}}
        response = None
        key = ""
        for attempt in range(attempts):
            key = key_pool.next()
            t_start = time.time()
            try:
                req = client.build_request("POST", UPSTREAM_URL, headers=build_headers(key), json=v3_payload)
                response = await client.send(req, stream=True)
            except Exception as e:
                if attempt + 1 < attempts:
                    wait_time = _backoff_delay(attempt)
                    logger.warning(f"fx: network exception on key={mask_key(key)}: {e}; retrying in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                raise
            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                if response.status_code == 429:
                    key_pool.mark_failed(key)
                    logger.warning(f"fx: 429 rate-limited on key={mask_key(key)}; backing off")
                else:
                    key_pool.mark_error(key)
                    logger.warning(f"fx: {response.status_code} error on key={mask_key(key)}; retrying next key")
                await response.aclose()
                await asyncio.sleep(_backoff_delay(attempt))
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
                return JSONResponse(status_code=response.status_code, content={"error": {"message": err_msg, "type": "upstream_error", "code": response.status_code}})
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
                    tool_calls_map[tc_id] = {"id": tc_id, "type": "function", "function": {"name": event.get("toolName", ""), "arguments": ""}}
                elif ev_type == "tool-input-delta":
                    tc_id = event.get("id")
                    if tc_id in tool_calls_map:
                        tool_calls_map[tc_id]["function"]["arguments"] += event.get("delta", "")
                elif ev_type == "tool-call":
                    tc_id = event.get("toolCallId")
                    inp = event.get("input")
                    args_str = inp if isinstance(inp, str) else json.dumps(inp or {})
                    tool_calls_map[tc_id] = {"id": tc_id, "type": "function", "function": {"name": event.get("toolName", ""), "arguments": args_str}}
                elif ev_type == "finish":
                    fr_obj = event.get("finishReason", {})
                    fr = fr_obj.get("unified") or fr_obj.get("raw") if isinstance(fr_obj, dict) else str(fr_obj or "stop")
                    if fr in ("tool-calls", "tool_calls"):
                        finish_reason = "tool_calls"
                    elif fr == "length":
                        finish_reason = "length"
                    usage_info = extract_usage(event)
            content_str = "".join(full_content)
            res_message: Dict[str, Any] = {"role": "assistant", "content": content_str if (content_str or not tool_calls_map) else None}
            if full_reasoning:
                res_message["reasoning_content"] = "".join(full_reasoning)
            if tool_calls_map:
                res_message["tool_calls"] = list(tool_calls_map.values())
            key_pool.mark_success(key, usage_info.get("total_tokens", 0), time.time() - t_start)
            return {"id": req_id, "object": "chat.completion", "created": created_ts, "model": model, "choices": [{"index": 0, "message": res_message, "finish_reason": finish_reason}], "usage": usage_info}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
        body = await request.json()
        auth = _extract_auth(request, authorization)
        try:
            result = await _proxy_chat(body, request, auth)
        except HTTPException as e:
            # map to OpenAI error shape
            return JSONResponse(status_code=e.status_code, content={"error": {"message": e.detail, "type": "invalid_request_error", "code": e.status_code}})
        return result

    @app.post("/v1/responses")
    async def responses(request: Request, authorization: Optional[str] = Header(None)):
        body = await request.json()
        chat_body = _responses_to_chat_body(body)
        auth = _extract_auth(request, authorization)
        try:
            result = await _proxy_chat(chat_body, request, auth)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"error": {"message": e.detail, "type": "invalid_request_error", "code": e.status_code}})
        if isinstance(result, StreamingResponse):
            return StreamingResponse(_chat_stream_to_response_stream(result, body), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
        if isinstance(result, Response):
            # error passthrough: map to Responses error shape
            try:
                err_data = json.loads(result.body.decode()) if hasattr(result, "body") else {}
            except Exception:
                err_data = {}
            return result
        return _chat_to_response(result, body)

    @app.post("/v1/messages")
    @app.post("/v1/messages/count_tokens")
    async def anthropic_messages(request: Request, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None, alias="x-api-key")):
        # count_tokens endpoint: approximate via chat usage? For now return estimated tokens without upstream call
        path = request.url.path
        if path.endswith("count_tokens"):
            body = await request.json()
            # handle both string and list content for anthropic messages
            def _count_text(c):
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    parts=[]
                    for b in c:
                        if isinstance(b, dict) and b.get("type")=="text":
                            parts.append(b.get("text",""))
                        elif isinstance(b, str):
                            parts.append(b)
                    return "".join(parts)
                return str(c or "")
            msgs = body.get("messages", [])
            txt = " ".join([_count_text(m.get("content", "")) for m in msgs])
            sys_txt = body.get("system", "")
            if isinstance(sys_txt, list):
                sys_txt = " ".join([b.get("text","") if isinstance(b, dict) else str(b) for b in sys_txt])
            else:
                sys_txt = str(sys_txt or "")
            # rough: 1 token ~ 4 chars
            cnt = max(1, len(txt) // 4 + len(sys_txt) // 4)
            return {"input_tokens": cnt}
        body = await request.json()
        # anthropic-version header is optional for our proxy, but log if missing
        chat_body = _anthropic_to_chat_body(body)
        # Anthropic sends x-api-key, not Authorization
        auth = _extract_auth(request, authorization) or (x_api_key or "").strip()
        # also allow Bearer in x-api-key
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        try:
            result = await _proxy_chat(chat_body, request, auth)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"type": "error", "error": {"type": "invalid_request_error", "message": e.detail}})
        if isinstance(result, StreamingResponse):
            return StreamingResponse(_chat_stream_to_anthropic_stream(result, body), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "anthropic-version": "2023-06-01"})
        if isinstance(result, Response):
            # map OpenAI error to Anthropic error shape
            status = getattr(result, "status_code", 500)
            try:
                raw = result.body.decode() if hasattr(result, "body") else b""
                j = json.loads(raw) if raw else {}
                msg = j.get("error", {}).get("message") or j.get("error") or raw.decode(errors="replace")[:500]
            except Exception:
                msg = "upstream error"
            return JSONResponse(status_code=status, content={"type": "error", "error": {"type": "api_error", "message": msg}})
        anthro = _chat_to_anthropic(result, body)
        return anthro

    # Anthropic also expects GET /v1/models compatible? Keep existing.

    return app


app = create_app()
