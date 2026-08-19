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
from fastapi.responses import StreamingResponse, JSONResponse, Response

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
    # Dedup while preserving order (same key in both env vars shouldn't double-count).
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


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
        if not self.keys:
            raise RuntimeError("key pool is empty")
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
# Converter helpers
# --------------------------------------------------------------------------- #

def _extract_text(content: Any) -> str:
    """Extract pure text from content, whether it's a string, list of blocks, or None."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "input_text", "output_text"):
                    texts.append(item.get("text", ""))
                elif "text" in item and isinstance(item["text"], str):
                    texts.append(item["text"])
            else:
                texts.append(str(item))
        return "".join(texts)
    if isinstance(content, dict):
        return content.get("text", "") if "text" in content else ""
    return str(content)


def convert_messages_to_v3(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OpenAI messages structure to Vercel AI SDK v3 Language Model prompt format."""
    prompt = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role in ("system", "developer"):
            prompt.append({
                "role": "system",
                "content": _extract_text(content)
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
                            if url.startswith("data:"):
                                header, _, b64 = url.partition(",")
                                media = header.split(";")[0].split(":")[1] if ":" in header else "image/png"
                                parts.append({"type": "file", "mediaType": media, "data": b64})
                            else:
                                ext = url.rsplit(".", 1)[-1].lower() if "." in url else "png"
                                media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
                                parts.append({"type": "file", "mediaType": media, "data": url})
                        elif "text" in item:
                            txt = str(item["text"])
                            parts.append({"type": "text", "text": txt if txt.strip() else " "})
                    elif isinstance(item, str):
                        parts.append({"type": "text", "text": item if item.strip() else " "})
                    else:
                        parts.append({"type": "text", "text": str(item) if str(item).strip() else " "})
            else:
                txt = _extract_text(content)
                parts.append({"type": "text", "text": txt if txt.strip() else " "})
            if not parts:
                parts.append({"type": "text", "text": " "})
            prompt.append({"role": "user", "content": parts})
        elif role == "assistant":
            parts = []
            text = _extract_text(content)
            if text and text.strip():
                parts.append({"type": "text", "text": text})
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
            if isinstance(content, str):
                out_str = content
            elif isinstance(content, (dict, list)):
                out_str = json.dumps(content)
            else:
                out_str = str(content or "")
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


# ── Responses API helpers ──

def _responses_content_to_chat(content: Any) -> Any:
    """Convert Responses API content blocks to OpenAI chat content blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _extract_text(content)

    parts: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            parts.append({"type": "text", "text": str(item)})
            continue

        item_type = item.get("type")
        if item_type in ("input_text", "output_text", "text", "refusal"):
            parts.append({"type": "text", "text": str(item.get("text", ""))})
        elif item_type in ("input_image", "image_url"):
            image_url = item.get("image_url") or item.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                parts.append({"type": "image_url", "image_url": {"url": image_url}})

    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "".join(part["text"] for part in parts)
    return parts


def responses_input_to_messages(input_value: Any, instructions: Optional[str] = None) -> List[Dict[str, Any]]:
    """Convert OpenAI Responses input/instructions to chat-completions messages."""
    messages: List[Dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_value, str):
        return messages + [{"role": "user", "content": input_value}]
    if not isinstance(input_value, list):
        return messages + [{"role": "user", "content": _extract_text(input_value)}]

    pending_content: List[Any] = []

    def flush_pending() -> None:
        if pending_content:
            messages.append({"role": "user", "content": _responses_content_to_chat(pending_content)})
            pending_content.clear()

    for item in input_value:
        if isinstance(item, str):
            pending_content.append({"type": "input_text", "text": item})
            continue
        if not isinstance(item, dict):
            pending_content.append({"type": "input_text", "text": str(item)})
            continue

        item_type = item.get("type")
        if item_type in ("message", "input_message") or item.get("role"):
            flush_pending()
            role = item.get("role", "user")
            messages.append({
                "role": role,
                "content": _responses_content_to_chat(item.get("content", "")),
            })
        elif item_type in ("input_text", "input_image", "image_url", "text"):
            pending_content.append(item)
        elif item_type == "function_call_output":
            flush_pending()
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "name": item.get("name", "tool"),
                "content": output,
            })
        elif item_type == "function_call":
            flush_pending()
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": item.get("call_id", str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }],
            })

    flush_pending()
    return messages


# ── Anthropic Messages helpers ──

def _anthropic_extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
            elif isinstance(b, str):
                out.append(b)
        return "".join(out)
    return str(content)


def anthropic_system_to_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        texts = []
        for block in system:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif "text" in block:
                    texts.append(str(block["text"]))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(t for t in texts if t)
    if isinstance(system, dict) and "text" in system:
        return str(system["text"])
    return str(system)


def anthropic_messages_to_chat(messages: List[Dict[str, Any]], system: Any = None) -> List[Dict[str, Any]]:
    """Convert Anthropic messages + system to OpenAI chat messages."""
    chat: List[Dict[str, Any]] = []
    sys_text = anthropic_system_to_text(system)
    if sys_text:
        chat.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        # string content -> pass through
        if isinstance(content, str):
            # Anthropic allows system role only via `system` param, but some clients send it in messages
            if role == "system":
                chat.append({"role": "system", "content": content})
            else:
                chat.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            chat.append({"role": role, "content": _anthropic_extract_text(content)})
            continue

        # content is list of blocks
        text_parts: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        tool_uses: List[Dict[str, Any]] = []
        thinking_text = ""

        for block in content:
            if not isinstance(block, dict):
                text_parts.append({"type": "text", "text": str(block)})
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append({"type": "text", "text": block.get("text", "")})
            elif btype == "thinking":
                thinking_text += block.get("thinking", "") or block.get("text", "")
            elif btype == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64":
                    media = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    url = f"data:{media};base64,{data}"
                    text_parts.append({"type": "image_url", "image_url": {"url": url}})
                elif src.get("type") == "url":
                    url = src.get("url", "")
                    if url:
                        text_parts.append({"type": "image_url", "image_url": {"url": url}})
                else:
                    # fallback: try direct url field
                    url = block.get("url") or src.get("url") or ""
                    if url:
                        text_parts.append({"type": "image_url", "image_url": {"url": url}})
            elif btype == "tool_use":
                tool_uses.append(block)
            elif btype == "tool_result":
                tool_results.append(block)
            elif btype == "image_url":
                url = block.get("image_url", {}).get("url") or block.get("url") or ""
                if url:
                    text_parts.append({"type": "image_url", "image_url": {"url": url}})

        # Anthropic packs tool_result blocks inside a user message alongside text.
        # OpenAI expects each tool_result as a separate tool message.
        if role == "user" and tool_results:
            # flush text parts as user message first if any
            if text_parts or thinking_text:
                combined = ""
                if thinking_text:
                    combined += thinking_text + "\n"
                if text_parts:
                    # keep structured if images present, else join texts
                    has_image = any(p.get("type") == "image_url" for p in text_parts)
                    if has_image:
                        chat.append({"role": "user", "content": text_parts})
                    else:
                        txt = "".join(p.get("text", "") for p in text_parts)
                        if txt.strip() or not tool_results:
                            chat.append({"role": "user", "content": txt})
                else:
                    if combined.strip():
                        chat.append({"role": "user", "content": combined})
            for tr in tool_results:
                tc_id = tr.get("tool_use_id", "")
                t_content = tr.get("content")
                if isinstance(t_content, list):
                    # flatten list of text/image blocks to string
                    flat = []
                    for c in t_content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            flat.append(c.get("text", ""))
                        elif isinstance(c, str):
                            flat.append(c)
                        else:
                            flat.append(str(c))
                    t_str = "".join(flat)
                elif isinstance(t_content, str):
                    t_str = t_content
                elif t_content is None:
                    t_str = ""
                else:
                    t_str = json.dumps(t_content, ensure_ascii=False)
                chat.append({"role": "tool", "tool_call_id": tc_id, "content": t_str})
            # if only text_parts and no tool_results, already handled
            if not text_parts and not tool_results and thinking_text:
                chat.append({"role": "user", "content": thinking_text})
        elif role == "assistant" and tool_uses:
            # assistant with tool_use blocks
            text_content = "".join(p.get("text", "") for p in text_parts) if text_parts else ""
            if thinking_text:
                text_content = thinking_text + ("\n" + text_content if text_content else "")
            tc_list = []
            for tu in tool_uses:
                args = tu.get("input", {})
                if not isinstance(args, dict):
                    try:
                        args = json.loads(args) if isinstance(args, str) else {}
                    except Exception:
                        args = {}
                tc_list.append({
                    "id": tu.get("id", str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": tu.get("name", ""),
                        "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                    }
                })
            msg_obj: Dict[str, Any] = {"role": "assistant", "content": text_content}
            if tc_list:
                msg_obj["tool_calls"] = tc_list
            chat.append(msg_obj)
            # if there were leftover text_parts with images, image is lost; but tool_use rarely mixes with images
        else:
            # plain text / image user or assistant without tool semantics
            if not text_parts and not tool_uses and not tool_results:
                txt = thinking_text or ""
                chat.append({"role": role, "content": txt})
            else:
                has_image = any(p.get("type") == "image_url" for p in text_parts)
                if has_image:
                    chat.append({"role": role, "content": text_parts})
                else:
                    txt = "".join(p.get("text", "") for p in text_parts)
                    if thinking_text:
                        txt = thinking_text + ("\n" + txt if txt else "")
                    # preserve assistant thinking as text if no other content
                    chat.append({"role": role, "content": txt if txt.strip() else " "})

    return chat


def anthropic_tools_to_chat(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Anthropic tool: {name, description, input_schema}
        if t.get("type") in ("web_search_20250305", "web_search", "code_execution", "bash_code_execution"):
            # server tools passthrough? skip for now, Vercel doesn't support them
            continue
        name = t.get("name", "")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or t.get("inputSchema") or {"type": "object", "properties": {}},
            }
        })
    return out if out else None


def anthropic_tool_choice_to_chat(tc: Any) -> Optional[Dict[str, Any]]:
    if not tc:
        return None
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "auto":
            return {"type": "auto"}
        if t == "any":
            return {"type": "required"}
        if t == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
        if t == "none":
            return {"type": "none"}
    return {"type": "auto"}

# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

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
