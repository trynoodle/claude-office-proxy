"""Anthropic Messages API ↔ OpenAI Chat Completions API 双向协议翻译。

支持:
- 工具定义: Anthropic "custom" type ↔ OpenAI "function" type
- 工具调用: tool_use content block ↔ tool_calls
- 工具结果: tool_result content block ↔ role: "tool" message
- System prompt: content block array ↔ string
- 流式/非流式 SSE
"""

import json
import re
import time
from typing import Any


def anth_to_openai(request_body: dict, routing_info: dict) -> dict:
    """将 Anthropic Messages API 请求转为 OpenAI Chat Completions 请求。

    Args:
        request_body: Anthropic 协议的请求 body
        routing_info: {"provider": "openai"|"anthropic", "base_url": ..., "api_key": ..., "model": ...}

    Returns:
        (openai_body, headers) — 可以直接用 httpx/aiohttp 发出的请求
    """
    model = routing_info["model"]
    messages = request_body.get("messages", [])
    system = request_body.get("system")
    tools = request_body.get("tools")
    tool_choice = request_body.get("tool_choice")
    max_tokens = request_body.get("max_tokens", 4096)
    stream = request_body.get("stream", True)

    # ---- System prompt ----
    system_str = None
    if isinstance(system, list):
        texts = [b.get("text", "") for b in system if isinstance(b, dict)]
        system_str = "\n".join(texts) if texts else None
    elif isinstance(system, str):
        system_str = system

    # ---- Messages ----
    openai_msgs = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, list):
            tool_results = []
            text_parts = []
            tool_calls_list = []  # assistant tool_use → OpenAI tool_calls
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type", "")
                if t == "text":
                    text_parts.append(block.get("text", ""))
                elif t == "tool_result":
                    tr_content = block.get("content", "")
                    if isinstance(tr_content, list):
                        tr_content = "\n".join(
                            x.get("text", "") for x in tr_content if isinstance(x, dict)
                        )
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": tr_content,
                    })
                elif t == "tool_use":
                    # Anthropic tool_use → OpenAI tool_calls（必须保留，否则下一轮 tool_result 找不到对应 tool_calls）
                    try:
                        args_str = json.dumps(block.get("input", {}), ensure_ascii=False)
                    except (TypeError, ValueError):
                        args_str = "{}"
                    tool_calls_list.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": args_str,
                        },
                    })

            # 先放 tool results（如果有），再放用户文本
            for tr in tool_results:
                openai_msgs.append(tr)
            # assistant 的工具调用 + 文本（合并为一条消息）
            if tool_calls_list:
                merged_content = "\n".join(text_parts) if text_parts else None
                openai_msgs.append({"role": "assistant", "content": merged_content, "tool_calls": tool_calls_list})
            elif text_parts:
                openai_msgs.append({"role": role, "content": "\n".join(text_parts)})
        else:
            openai_msgs.append({"role": role, "content": str(content)})

    # ---- Build body ----
    oai: dict[str, Any] = {
        "model": model,
        "messages": openai_msgs,
        "stream": stream,
    }

    if system_str:
        oai["messages"] = [{"role": "system", "content": system_str}] + oai["messages"]

    if max_tokens:
        # DeepSeek V4 系列 reasoning token 会计入 max_tokens，需要给足预算
        if "deepseek" in model.lower() and "v4" in model.lower():
            max_tokens = max(max_tokens, 1024)
        oai["max_tokens"] = max_tokens

    # ---- Tools ----
    if tools:
        oai_tools = []
        for t in tools:
            schema = t.get("input_schema") or {}
            # 确保是最小合法的 JSON Schema（某些工具 schema 为 {"type": null} 会被拒绝）
            if not isinstance(schema, dict) or schema.get("type") is None:
                schema = {"type": "object"}
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": schema,
                },
            })
        oai["tools"] = oai_tools

        if tool_choice:
            tc = tool_choice
            if isinstance(tc, dict):
                if tc.get("type") == "any":
                    oai["tool_choice"] = "required"
                elif tc.get("type") == "tool" and tc.get("name"):
                    oai["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
                elif tc.get("type") == "auto":
                    oai["tool_choice"] = "auto"
            elif tc == "any":
                oai["tool_choice"] = "required"
            elif tc == "auto":
                oai["tool_choice"] = "auto"

    return oai


def openai_to_anth(resp_json: dict, req_model: str, req_id: str = "") -> dict:
    """将 OpenAI Chat Completion 响应转为 Anthropic Message。

    Args:
        resp_json: OpenAI API 返回的 JSON
        req_model: Excel 请求时的模型名（用于响应中回写）
        req_id: 请求 ID（透传）

    Returns:
        Anthropic 协议的 message 字典
    """
    choice = (resp_json.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "") or ""
    reasoning = message.get("reasoning_content", "") or ""
    tool_calls = message.get("tool_calls", [])
    finish = choice.get("finish_reason", "stop")
    usage = resp_json.get("usage", {})

    anth: dict[str, Any] = {
        "id": req_id or resp_json.get("id", f"msg_{int(time.time() * 1e6)}"),
        "type": "message",
        "role": "assistant",
        "model": req_model,
        "content": [],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0),
        },
    }

    # DeepSeek V4: 思考内容放在 reasoning_content 里
    if reasoning:
        anth["content"].append({"type": "thinking", "thinking": reasoning})

    # 正文
    if content:
        anth["content"].append({"type": "text", "text": content})

    # 工具调用
    for tc in tool_calls:
        func = tc.get("function", {})
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        anth["content"].append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{int(time.time() * 1e6)}"),
            "name": func.get("name", ""),
            "input": args,
        })

    # finish_reason 映射
    mapping = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}
    anth["stop_reason"] = mapping.get(finish, "end_turn")

    return anth


def strip_model_suffix(model: str) -> str:
    """剥离模型名的日期后缀: claude-opus-4-7-20250820 → claude-opus-4-7"""
    return re.sub(r"-\d{8}$", "", model)


def parse_oai_sse(text: str) -> str:
    """解析 OpenAI SSE 流式响应，合并为单个 JSON。"""
    lines = text.strip().split("\n")
    parts = [l[5:].strip() for l in lines if l.startswith("data:") and l[5:].strip() not in ("[DONE]", "")]
    if not parts:
        return "{}"

    merged: dict[str, Any] = {"finish_reason": "stop"}
    for p in parts:
        try:
            chunk = json.loads(p)
            choices = chunk.get("choices", [])
            if choices and "delta" in choices[0]:
                d = choices[0]["delta"]
                if "content" in d and d["content"]:
                    merged["content"] = merged.get("content", "") + d["content"]
                if "tool_calls" in d:
                    for tc in d["tool_calls"]:
                        idx = tc.get("index", 0)
                        while len(merged.get("tool_calls", [])) <= idx:
                            merged.setdefault("tool_calls", []).append({
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                        if "id" in tc:
                            merged["tool_calls"][idx]["id"] = tc["id"]
                        if "function" in tc:
                            f = tc["function"]
                            if "name" in f and f["name"]:
                                merged["tool_calls"][idx]["function"]["name"] = f["name"]
                            if "arguments" in f:
                                merged["tool_calls"][idx]["function"]["arguments"] += f["arguments"]
                merged["finish_reason"] = choices[0].get("finish_reason", "stop")
        except json.JSONDecodeError:
            pass

    return json.dumps({
        "choices": [{"message": merged, "finish_reason": merged.pop("finish_reason", "stop")}],
        "usage": {},
    }, ensure_ascii=False)
