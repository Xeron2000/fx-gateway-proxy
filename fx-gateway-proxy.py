#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.115.0",
#     "uvicorn[standard]>=0.30.0",
#     "httpx>=0.27.0",
# ]
# ///

import os
from pathlib import Path

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://ai-gateway.vercel.sh/v3/ai/language-model")
USER_AGENT = os.environ.get("FX_USER_AGENT", "fx/0.0.3")
DEFAULT_KEY_PATH = Path.home() / ".fx" / "api-key"
DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "18080"))

def get_api_key(explicit_key: str = "") -> str:
    """Resolve API key from explicit param, environment, or ~/.fx/api-key file."""
    if explicit_key and explicit_key.lower() not in ("dummy", "none", "null", "placeholder", "ollama"):
        return explicit_key.strip()
    if "AI_GATEWAY_API_KEY" in os.environ and os.environ["AI_GATEWAY_API_KEY"].strip():
        return os.environ["AI_GATEWAY_API_KEY"].strip()
    if DEFAULT_KEY_PATH.exists():
        try:
            return DEFAULT_KEY_PATH.read_text().strip()
        except Exception:
            pass
    return ""

import json
import uuid
from typing import Any, Dict, List, Optional

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
                            url = item.get("image_url", {}).get("url", "")
                            # AI SDK v3 expects {type: "file", mediaType, data}; data may be a URL or base64.
                            # ponytail: glm-5.2 has no vision, but keep the conversion correct for models that do.
                            if url.startswith("data:"):
                                header, _, b64 = url.partition(",")
                                media = header.split(";")[0].split(":")[1] if ":" in header else "image/png"
                                parts.append({"type": "file", "mediaType": media, "data": b64})
                            else:
                                media = "image/png"
                                low = url.lower()
                                if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                                    ext = low.rsplit(".", 1)[-1]
                                    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
                                parts.append({"type": "file", "mediaType": media, "data": url})
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
                    "output": {"type": "text", "value": out_str}
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
    effort_lower = effort.lower()
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

import time
import json
import uuid
import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse




logger = logging.getLogger("fx-gateway-proxy")

http_client: Optional[httpx.AsyncClient] = None
MAX_RETRIES = int(os.environ.get("FX_MAX_RETRIES", "5"))
BASE_DELAY = float(os.environ.get("FX_BASE_DELAY", "0.8"))
MAX_DELAY = float(os.environ.get("FX_MAX_DELAY", "20.0"))
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff: min(BASE_DELAY * 2^attempt, MAX_DELAY)."""
    return min(BASE_DELAY * (2 ** attempt), MAX_DELAY)

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

def create_app() -> FastAPI:
    app = FastAPI(
        title="FX Gateway Proxy",
        description="OpenAI-compatible reverse proxy for Vercel AI Gateway FX Free Promo Pool",
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
        return {"status": "ok", "service": "fx-gateway-proxy"}

    @app.get("/v1/models")
    async def list_models():
        models = [
            {"id": "zai/glm-5.2", "object": "model", "owned_by": "zai", "permission": []},
            {"id": "zai/glm-5.2-fast", "object": "model", "owned_by": "zai", "permission": []},
        ]
        return {"object": "list", "data": models}

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

        # Resolve API Key
        incoming_key = ""
        if authorization and authorization.startswith("Bearer "):
            incoming_key = authorization[7:].strip()
        api_key = get_api_key(incoming_key)

        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing API Key. Provide via Authorization header, AI_GATEWAY_API_KEY env, or ~/.fx/api-key file."
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

        v3_headers = {
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
        
        # Passthrough tracing headers if provided by client
        for th in ("traceparent", "tracestate", "x-request-id", "x-b3-traceid"):
            if th in headers_in:
                v3_headers[th] = headers_in[th]

        req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        client = http_client or httpx.AsyncClient(timeout=300.0)

        async def stream_generator() -> AsyncGenerator[str, None]:
            attempt = 0
            while True:
                try:
                    async with client.stream(
                        "POST",
                        UPSTREAM_URL,
                        headers=v3_headers,
                        json=v3_payload,
                    ) as response:
                        if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                            wait_time = _backoff_delay(attempt)
                            logger.warning(f"Upstream rate-limited ({response.status_code}), auto-retrying in {wait_time}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                            await asyncio.sleep(wait_time)
                            attempt += 1
                            continue

                        if response.status_code != 200:
                            err_bytes = await response.aread()
                            err_msg = err_bytes.decode(errors="replace")
                            logger.error(f"Gateway error ({response.status_code}): {err_msg}")
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
                        return
                except asyncio.CancelledError:
                    logger.info("Stream task cancelled by client.")
                    raise
                except Exception as e:
                    if attempt < MAX_RETRIES:
                        wait_time = _backoff_delay(attempt)
                        logger.warning(f"Stream network exception: {e}, auto-retrying in {wait_time}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                        await asyncio.sleep(wait_time)
                        attempt += 1
                        continue
                    raise

        if stream:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        # Non-streaming implementation with automatic retry
        attempt = 0
        while True:
            full_content = []
            full_reasoning = []
            tool_calls_map: Dict[str, Dict[str, Any]] = {}
            usage_info = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0}
            }

            async with client.stream(
                "POST",
                UPSTREAM_URL,
                headers=v3_headers,
                json=v3_payload,
            ) as response:
                if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    wait_time = _backoff_delay(attempt)
                    logger.warning(f"Upstream rate-limited ({response.status_code}), auto-retrying in {wait_time}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                    await asyncio.sleep(wait_time)
                    attempt += 1
                    continue

                if response.status_code != 200:
                    err_bytes = await response.aread()
                    err_str = err_bytes.decode(errors="replace")
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

    return app

app = create_app()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="FX Gateway Proxy")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", 18080)), help="Port to bind (default: 18080)")
    args = parser.parse_args()

    logger.info(f"Starting FX Gateway Proxy on {args.host}:{args.port}...")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
