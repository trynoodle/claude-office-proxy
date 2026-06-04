"""Anthropic Message → SSE 事件流构造器。

生成符合 Anthropic Streaming Messages API 格式的 SSE 事件。
"""

import json
from typing import Any


def message_to_sse(msg: dict) -> bytes:
    """将一个完整的 Anthropic Message 转为 SSE 事件流。

    Args:
        msg: Anthropic 协议的 message 字典

    Returns:
        SSE 格式的字节流（可直接作为 HTTP 响应 body 发送）
    """
    parts: list[str] = []
    model = msg.get("model", "")
    mid = msg.get("id", "")
    usage = msg.get("usage", {})
    content = msg.get("content") or []
    stop = msg.get("stop_reason", "end_turn")
    stop_seq = msg.get("stop_sequence")

    it = usage.get("input_tokens", 0)
    ot = usage.get("output_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    cc = usage.get("cache_creation_input_tokens", 0)

    # ---- message_start ----
    ms = json.dumps({
        "type": "message_start",
        "message": {
            "id": mid, "type": "message", "role": "assistant",
            "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {
                "input_tokens": it, "output_tokens": 0,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        },
    }, ensure_ascii=False)
    parts.append(f"event: message_start\ndata: {ms}")

    # ---- content blocks ----
    for idx, block in enumerate(content):
        ct = block.get("type", "text")

        if ct == "thinking":
            _add_sse_block(parts, idx, "thinking", {
                "type": "thinking",
                "thinking": block.get("thinking", ""),
                "signature": block.get("signature", ""),
            }, {
                "type": "thinking_delta",
                "thinking": block.get("thinking", ""),
            })

        elif ct == "tool_use":
            _add_sse_block(parts, idx, "tool_use", {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": {},  # Anthropic SDK 要求此字段存在，否则 tool_use 块被静默丢弃
            }, {
                "type": "input_json_delta",
                "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
            })

        else:  # text
            _add_sse_block(parts, idx, "text",
                {"type": "text", "text": ""},
                {"type": "text_delta", "text": block.get("text", "")},
            )

        # content_block_stop
        cbs_data = json.dumps({"type": "content_block_stop", "index": idx}, ensure_ascii=False)
        parts.append(f"\n\nevent: content_block_stop\ndata: {cbs_data}")

    # ---- message_delta ----
    md_data = json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": stop, "stop_sequence": stop_seq},
        "usage": {"output_tokens": ot},
    }, ensure_ascii=False)
    parts.append(f"\n\nevent: message_delta\ndata: {md_data}")

    # ---- message_stop ----
    parts.append(f"\n\nevent: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n")

    return "".join(parts).encode("utf-8")


def _add_sse_block(parts: list[str], idx: int, block_type: str, block_attrs: dict, delta_attrs: dict) -> None:
    """添加一个 SSE content block（start + delta）。"""
    cbs_data = json.dumps({
        "type": "content_block_start",
        "index": idx,
        "content_block": block_attrs,
    }, ensure_ascii=False)
    parts.append(f"\n\nevent: content_block_start\ndata: {cbs_data}")

    delta_data = json.dumps({
        "type": "content_block_delta",
        "index": idx,
        "delta": delta_attrs,
    }, ensure_ascii=False)
    parts.append(f"\n\nevent: content_block_delta\ndata: {delta_data}")
