"""协议翻译器单元测试。"""

from claude_office_proxy.translator import (
    anth_to_openai,
    openai_to_anth,
    strip_model_suffix,
    parse_oai_sse,
)


def test_strip_model_suffix():
    assert strip_model_suffix("claude-opus-4-7-20250820") == "claude-opus-4-7"
    assert strip_model_suffix("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_anth_to_openai_basic():
    req = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    route = {"provider": "openai", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-test", "model": "deepseek-v4-pro"}
    result = anth_to_openai(req, route)

    assert result["model"] == "deepseek-v4-pro"
    assert result["stream"] is True
    assert len(result["messages"]) == 1
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][0]["content"] == "hi"


def test_anth_to_openai_with_tools():
    req = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "write hello in A1"}],
        "max_tokens": 100,
        "tools": [{
            "name": "set_cell_range",
            "description": "Write values to cells",
            "input_schema": {"type": "object", "properties": {}},
        }],
    }
    route = {"provider": "openai", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-test", "model": "deepseek-v4-pro"}
    result = anth_to_openai(req, route)

    assert len(result["tools"]) == 1
    assert result["tools"][0]["type"] == "function"
    assert result["tools"][0]["function"]["name"] == "set_cell_range"
    assert result["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_anth_to_openai_with_system():
    req = {
        "model": "claude-opus-4-7",
        "system": [{"type": "text", "text": "You are helpful."}],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    route = {"provider": "openai", "base_url": "https://test", "api_key": "k",
             "model": "test-model"}
    result = anth_to_openai(req, route)

    assert result["messages"][0]["role"] == "system"
    assert result["messages"][0]["content"] == "You are helpful."


def test_openai_to_anth_basic():
    resp = {
        "id": "msg-123",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    msg = openai_to_anth(resp, "claude-opus-4-7")
    assert msg["role"] == "assistant"
    assert msg["model"] == "claude-opus-4-7"
    assert msg["stop_reason"] == "end_turn"
    assert len(msg["content"]) == 1
    assert msg["content"][0]["type"] == "text"
    assert msg["content"][0]["text"] == "Hello!"


def test_openai_to_anth_with_tools():
    resp = {
        "id": "msg-123",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "set_cell_range",
                        "arguments": '{"cells": [{"address": "A1", "value": "hello"}]}',
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }
    msg = openai_to_anth(resp, "claude-opus-4-7")

    assert msg["stop_reason"] == "tool_use"
    assert msg["content"][0]["type"] == "tool_use"
    assert msg["content"][0]["name"] == "set_cell_range"
    assert msg["content"][0]["input"] == {"cells": [{"address": "A1", "value": "hello"}]}


def test_openai_to_anth_with_reasoning():
    resp = {
        "id": "msg-123",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello!",
                "reasoning_content": "The user said hi...",
            },
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    msg = openai_to_anth(resp, "claude-opus-4-7")
    types = [c["type"] for c in msg["content"]]
    assert "thinking" in types
    assert "text" in types


def test_parse_oai_sse():
    sse_text = """data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}

data: {"choices":[{"delta":{"content":" world"},"index":0}]}

data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}

data: [DONE]"""

    result = parse_oai_sse(sse_text)
    import json
    d = json.loads(result)
    assert d["choices"][0]["message"]["content"] == "Hello world"
