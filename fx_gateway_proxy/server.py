import time
import json
import uuid
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .config import UPSTREAM_URL, USER_AGENT, get_api_key
from .converter import (
    convert_messages_to_v3,
    convert_tools_to_v3,
    convert_tool_choice_to_v3,
    map_reasoning_effort,
    extract_usage,
)

logger = logging.getLogger("fx-gateway-proxy")

http_client: Optional[httpx.AsyncClient] = None

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
            try:
                async with client.stream(
                    "POST",
                    UPSTREAM_URL,
                    headers=v3_headers,
                    json=v3_payload,
                ) as response:
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
            except asyncio.CancelledError:
                logger.info("Stream task cancelled by client.")
                raise

        if stream:
            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        # Non-streaming implementation
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
            if response.status_code != 200:
                err_text = await response.aread()
                raise HTTPException(status_code=response.status_code, detail=err_text.decode())

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

        res_message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(full_content) or None
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
