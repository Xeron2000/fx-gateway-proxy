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
