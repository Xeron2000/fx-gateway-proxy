import json
import uuid
from typing import Any, Dict, List, Optional


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
