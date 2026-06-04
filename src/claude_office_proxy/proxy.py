"""Claude for Office 代理服务器主程序。

架构: Excel/PPT/Word WebView2 --HTTPS--> proxy:8766 --HTTP--> 国产模型 API

功能:
- HTTPS TLS 终止（mkcert 自签证书）
- CORS 头注入（pivot.claude.ai）
- Anthropic → OpenAI 协议翻译（含工具调用）
- 多提供商路由
- SSE 事件流响应
"""

import json
import os
import re
import ssl
import time
from datetime import datetime
from typing import Any

import aiohttp
from aiohttp import web

from .translator import anth_to_openai, openai_to_anth, parse_oai_sse, strip_model_suffix
from .sse import message_to_sse

# ---- CORS 头（Claude for Office 必需） ----
_CORS = {
    "Access-Control-Allow-Origin": "https://pivot.claude.ai",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": (
        "content-type,authorization,x-api-key,anthropic-version,"
        "anthropic-dangerous-direct-browser-access,"
        "x-stainless-arch,x-stainless-helper-method,x-stainless-lang,"
        "x-stainless-os,x-stainless-package-version,x-stainless-retry-count,"
        "x-stainless-runtime,x-stainless-runtime-version,x-stainless-timeout"
    ),
    "Access-Control-Max-Age": "86400",
    "anthropic-dangerous-direct-browser-access": "true",
}

# ---- Hop-by-hop headers (不转发给后端) ----
_HOP = frozenset({"host", "content-length", "transfer-encoding", "connection", "accept-encoding"})


def build_app(config: dict, log_path: str | None = None) -> web.Application:
    """构建 aiohttp Application。

    Args:
        config: YAML 配置字典
        log_path: 日志文件路径（None 则仅 stdout）
    """
    models_list: list[str] = config.get("models", [])
    providers: dict = config.get("providers", {})
    routing: dict = config.get("routing", {})

    # 构建路由映射: model_id → (provider_name, model_name)
    # 用完整 model ID 做 key，不是 strip 后的短名
    route_map: dict[str, tuple[str, str]] = {}
    model_meta: dict[str, str] = {}  # model_id → display_name
    for source, dest in routing.items():
        route_map[source] = (dest.get("provider", ""), dest.get("model", ""))
        # 同时注册 strip 后的短名（兼容插件可能发短名的场景）
        short = strip_model_suffix(source)
        if short != source and short not in route_map:
            route_map[short] = (dest.get("provider", ""), dest.get("model", ""))
        model_meta[source] = dest.get("display_name", source)

    # 构建模型列表响应（符合 Anthropic API 格式——id 必须有日期后缀）
    model_list_data = {
        "data": [
            {
                "id": m,
                "type": "model",
                "display_name": model_meta.get(m, m),
                "created_at": "2025-01-01T00:00:00Z",
            }
            for m in models_list
        ],
        "has_more": False,
        "first_id": models_list[0] if models_list else None,
        "last_id": models_list[-1] if models_list else None,
    }

    async def handler(request: web.Request) -> web.StreamResponse:
        method = request.method
        path = request.path.rstrip("/") or "/"
        raw_body = await request.read()

        if log_path:
            log(log_path, f"{method} {request.url.path} [{len(raw_body)}b]")

        # ---- OPTIONS (CORS preflight) ----
        if method == "OPTIONS":
            return web.Response(status=204, headers=dict(_CORS))

        # ---- GET / (health check) ----
        if method == "GET" and path == "/":
            return web.json_response({"status": "ok"}, headers=dict(_CORS))

        # ---- GET /v1/models ----
        if method == "GET" and path == "/v1/models":
            return web.json_response(model_list_data, headers=dict(_CORS))

        # ---- POST /v1/messages (核心) ----
        if path == "/v1/messages":
            return await _handle_messages(request, raw_body, providers, route_map, log_path)

        # ---- Catch-all forward (for other paths) ----
        return web.json_response({"error": "not found"}, status=404, headers=dict(_CORS))

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def _handle_messages(
    request: web.Request,
    raw_body: bytes,
    providers: dict,
    route_map: dict,
    log_path: str | None,
) -> web.StreamResponse:
    """处理 POST /v1/messages。"""
    # 解析请求
    try:
        req_data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        req_data = json.loads(raw_body.decode("utf-8", errors="replace"))

    req_model = req_data.get("model", "")
    route = _lookup_route(req_model, route_map)
    provider_name, model_name = route

    provider = providers.get(provider_name, {})
    provider_type = provider.get("type", "openai")
    base_url = provider.get("base_url", "")
    api_key = provider.get("api_key", "")

    if not provider_name or not base_url:
        return web.json_response(
            {"type": "error", "error": {"message": f"未配置模型路由: {req_model}"}},
            status=500, headers=dict(_CORS),
        )

    # 日志：显示消息预览和工具数量
    preview = _msg_preview(req_data.get("messages", []))
    tool_count = len(req_data.get("tools", []))
    tool_names = [t.get("name", "") for t in req_data.get("tools", [])]

    if log_path:
        log(log_path,
            f"  model={req_model} -> [{provider_name}] {model_name} "
            f"tools={tool_count} msg={preview}"
        )

    router_info = {
        "provider": provider_type,
        "base_url": base_url,
        "api_key": api_key,
        "model": model_name,
    }

    # ---- Anthropic 直通（RouterTeam Claude）----
    if provider_type == "anthropic":
        return await _forward_anthropic(request, raw_body, router_info, req_model, log_path)

    # ---- OpenAI 路径（DeepSeek / Mimo / GPT-5.5）----
    try:
        oai_body = anth_to_openai(req_data, router_info)
    except Exception as e:
        if log_path:
            log(log_path, f"  TRANSLATE ERR: {e}")
        return web.json_response(
            {"type": "error", "error": {"message": f"translate: {e}"}},
            status=500, headers=dict(_CORS),
        )

    if log_path:
        log(log_path, f"  -> {base_url}/chat/completions [{len(json.dumps(oai_body))}b]")

    # 流式转发：OpenAI SSE → Anthropic SSE 实时翻译
    fwd_headers = _forward_headers(request)
    return await _forward_openai(
        base_url, api_key, fwd_headers, oai_body, req_model,
        req_data.get("id", ""), log_path, request,
    )


async def _forward_openai(
    base_url: str,
    api_key: str,
    fwd_headers: dict,
    oai_body: dict,
    req_model: str,
    req_id: str,
    log_path: str | None,
    request: web.Request,
) -> web.StreamResponse:
    """转发请求到 OpenAI 兼容 API，流式翻译响应为 Anthropic SSE。"""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 强制流式：实时翻译每个 chunk，让 thinking/文本实时显示
    oai_body["stream"] = True
    body = json.dumps(oai_body, ensure_ascii=False).encode("utf-8")

    rsp = web.StreamResponse(status=200, reason="OK", headers=dict(
        _CORS, **{"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    ))
    await rsp.prepare(request)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(url, headers=headers, data=body) as resp:
                if log_path:
                    log(log_path, f"  <- {resp.status} (streaming)")

                if resp.status != 200:
                    err_text = await resp.text()
                    err_text = err_text[:500]
                    if log_path:
                        log(log_path, f"  ERR: {err_text}")
                    # 发 Anthropic SSE 格式的错误，否则客户端显示 "request ended without sending any chunks"
                    msg_id = req_id or f"msg_{int(time.time() * 1e6)}"
                    ms = json.dumps({
                        "type": "message_start",
                        "message": {"id": msg_id, "type": "message", "role": "assistant",
                                    "model": req_model, "content": [],
                                    "stop_reason": None, "stop_sequence": None,
                                    "usage": {"input_tokens": 0, "output_tokens": 0}},
                    }, ensure_ascii=False)
                    cbs = json.dumps({
                        "type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }, ensure_ascii=False)
                    cbd = json.dumps({
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta",
                                  "text": f"API 错误: {err_text[:200]}"},
                    }, ensure_ascii=False)
                    cbs_stop = json.dumps({"type": "content_block_stop", "index": 0}, ensure_ascii=False)
                    md = json.dumps({
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 0},
                    }, ensure_ascii=False)
                    ms_stop = json.dumps({"type": "message_stop"}, ensure_ascii=False)
                    sse_err = (f"event: message_start\ndata: {ms}\n\n"
                               f"event: content_block_start\ndata: {cbs}\n\n"
                               f"event: content_block_delta\ndata: {cbd}\n\n"
                               f"event: content_block_stop\ndata: {cbs_stop}\n\n"
                               f"event: message_delta\ndata: {md}\n\n"
                               f"event: message_stop\ndata: {ms_stop}\n\n")
                    await rsp.write(sse_err.encode("utf-8"))
                    await rsp.write_eof()
                    return rsp

                # 流式读取 & 实时翻译
                await _stream_translate(resp, rsp, req_model, req_id, log_path)

    except aiohttp.ClientConnectorError:
        if log_path:
            log(log_path, "  ERR: connection failed")
        await rsp.write(
            json.dumps({"type": "error", "error": {"message": "connection failed"}},
                       ensure_ascii=False).encode("utf-8")
        )
        await rsp.write_eof()
    except Exception as e:
        if log_path:
            log(log_path, f"  ERR: {e}")
        await rsp.write(
            json.dumps({"type": "error", "error": {"message": str(e)}},
                       ensure_ascii=False).encode("utf-8")
        )
        await rsp.write_eof()

    return rsp


async def _stream_translate(
    upstream: aiohttp.ClientResponse,
    downstream: web.StreamResponse,
    req_model: str,
    req_id: str,
    log_path: str | None,
) -> None:
    """逐行读取 OpenAI SSE 流，翻译为 Anthropic SSE 并实时写出。

    Anthropic SSE 协议要求 content blocks 逐个打开、逐个关闭（不能交错）:
      message_start → content_block_start(A) → delta(A) → stop(A)
                   → content_block_start(B) → delta(B) → stop(B)
                   → ... → message_delta → message_stop

    因此我们在检测到 block 类型切换时，必须先关闭当前 block 再打开下一个。
    """
    msg_id = req_id or f"msg_{int(time.time() * 1e6)}"

    # 追踪当前打开的 block（Anthropic 协议下同一时刻只有一个 block 打开）
    block_index = 0
    current_block_key: str | None = None  # 当前打开的 block 标识 (thinking/text/tool_use_N)
    content_sent: list[str] = []  # 已发送过的 block key 列表（保持顺序）

    # 累积器
    tc_buf: dict[int, dict] = {}  # tool_call index → {id, name, arguments}

    finish_reason = ""  # 空字符串表示未收到 finish（模型可能返回 "stop"，需要区分）
    _stream_done = False  # 标志位：已收到 finish，需要退出外层 for 循环
    prompt_tokens = 0
    completion_tokens = 0
    msg_started = False

    async def _close_and_flush() -> None:
        """关闭当前 block 并 flush 到下游。"""
        nonlocal current_block_key
        if current_block_key is not None:
            bi = content_sent.index(current_block_key) if current_block_key in content_sent else -1
            if bi >= 0:
                cbs_stop = json.dumps({
                    "type": "content_block_stop", "index": bi,
                }, ensure_ascii=False)
                await _send(f"event: content_block_stop\ndata: {cbs_stop}\n\n".encode("utf-8"))
            current_block_key = None

    async def _start_block(block_type: str, block_name: str, block_extra: dict) -> None:
        """打开一个新的 content block（先关闭当前打开的）。

        如果 block_name 之前已打开过（因 chunk 交错导致重复打开），
        复用旧 index，保证 content_block_start 和 content_block_delta 的 index 一致。
        """
        nonlocal block_index, current_block_key
        await _close_and_flush()

        # 复用旧 index（否则 start 用新 index、delta 用旧 index，SDK 丢弃整个 block）
        if block_name in content_sent:
            idx = content_sent.index(block_name)
        else:
            idx = block_index
            content_sent.append(block_name)
            block_index = len(content_sent)

        cbs = json.dumps({
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": block_type, **block_extra},
        }, ensure_ascii=False)
        await _send(f"event: content_block_start\ndata: {cbs}\n\n".encode("utf-8"))

        current_block_key = block_name  # type: ignore[assignment]

    # 读取 SSE 行
    buffer = ""

    async def _send(data: bytes) -> None:
        """写入数据并立即刷到客户端。aiohttp write() 有 64KB 内部缓冲，小 chunk 不会自动触发 TCP 发送，必须 drain()。"""
        await downstream.write(data)
        await downstream.drain()

    try:
        async for chunk in upstream.content.iter_chunked(8192):
            text = chunk.decode("utf-8", errors="replace")
            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break

                try:
                    chunk_json = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk_json.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                fr = choices[0].get("finish_reason") or ""
                usage = chunk_json.get("usage", {})

                if usage:
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)

                # ---- message_start (只发一次) ----
                if not msg_started:
                    ms = json.dumps({
                        "type": "message_start",
                        "message": {
                            "id": msg_id, "type": "message", "role": "assistant",
                            "model": req_model, "content": [],
                            "stop_reason": None, "stop_sequence": None,
                            "usage": {"input_tokens": prompt_tokens, "output_tokens": 0,
                                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                        },
                    }, ensure_ascii=False)
                    await _send(f"event: message_start\ndata: {ms}\n\n".encode("utf-8"))
                    msg_started = True

                # ---- reasoning_content (DeepSeek thinking) ----
                rc = delta.get("reasoning_content", "") or ""
                if rc:
                    if current_block_key != "thinking":
                        await _start_block("thinking", "thinking",
                                           {"thinking": "", "signature": ""})
                    bi = content_sent.index("thinking")
                    cbd = json.dumps({
                        "type": "content_block_delta", "index": bi,
                        "delta": {"type": "thinking_delta", "thinking": rc},
                    }, ensure_ascii=False)
                    await _send(f"event: content_block_delta\ndata: {cbd}\n\n".encode("utf-8"))

                # ---- text content ----
                c = delta.get("content", "") or ""
                if c:
                    if current_block_key != "text":
                        await _start_block("text", "text", {"text": ""})
                    bi = content_sent.index("text")
                    cbd = json.dumps({
                        "type": "content_block_delta", "index": bi,
                        "delta": {"type": "text_delta", "text": c},
                    }, ensure_ascii=False)
                    await _send(f"event: content_block_delta\ndata: {cbd}\n\n".encode("utf-8"))

                # ---- tool calls ----
                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    if idx not in tc_buf:
                        tc_buf[idx] = {"id": "", "name": "", "arguments": ""}
                    if "id" in tc and tc["id"]:
                        tc_buf[idx]["id"] = tc["id"]
                    if "function" in tc:
                        fn = tc["function"]
                        if "name" in fn and fn["name"]:
                            tc_buf[idx]["name"] = fn["name"]
                            tkey = f"tool_use_{idx}"
                            # 如果 id 还没收到，生成一个（部分 provider 流式不返回 id）
                            tool_id = tc_buf[idx]["id"] or f"toolu_{fn['name']}_{idx}_{int(time.time() * 1e6)}"
                            if current_block_key != tkey:
                                await _start_block("tool_use", tkey, {
                                    "id": tool_id,
                                    "name": fn["name"],
                                    "input": {},
                                })
                        if "arguments" in fn:
                            tc_buf[idx]["arguments"] += fn["arguments"]
                            tkey = f"tool_use_{idx}"
                            # 防御：arguments 可能先于 name 到达（DeepSeek 流式行为）
                            if tkey not in content_sent:
                                tool_id = tc_buf[idx]["id"] or f"toolu_unknown_{idx}_{int(time.time() * 1e6)}"
                                tool_name = tc_buf[idx]["name"] or "unknown"
                                if log_path:
                                    log(log_path, f"    ⚠ tool_use_{idx} arguments before name, using name={tool_name}")
                                await _start_block("tool_use", tkey, {
                                    "id": tool_id,
                                    "name": tool_name,
                                    "input": {},
                                })
                            bi = content_sent.index(tkey)
                            cbd = json.dumps({
                                "type": "content_block_delta", "index": bi,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": fn["arguments"],
                                },
                            }, ensure_ascii=False)
                            await _send(f"event: content_block_delta\ndata: {cbd}\n\n".encode("utf-8"))

                # ---- finish ----
                if fr:
                    finish_reason = fr
                    _stream_done = True
                    break  # 跳出 SSE line while 循环

            # 跳出外层 for 循环
            if _stream_done:
                break

        # ---- 收尾：关闭当前 block + message_delta + message_stop ----
        await _close_and_flush()

        fr_map = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}
        stop_reason = fr_map.get(finish_reason, "end_turn")
        md = json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": completion_tokens},
        }, ensure_ascii=False)
        await _send(f"event: message_delta\ndata: {md}\n\n".encode("utf-8"))
        await _send(f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n".encode("utf-8"))
        await downstream.write_eof()

        if log_path:
            tc_total = len(tc_buf)
            buf_total = sum(len("".join(str(v) for v in tc.values())) for tc in tc_buf.values())
            log(log_path,
                f"  -> SSE stream done: blocks={len(content_sent)} "
                f"tools={tc_total} finish={finish_reason}"
            )

    except (ConnectionResetError, BrokenPipeError, OSError):
        if log_path:
            log(log_path, "  -> SSE stream aborted (client disconnected)")
        try:
            await downstream.write_eof()
        except Exception:
            pass


async def _forward_anthropic(
    request: web.Request,
    raw_body: bytes,
    router_info: dict,
    req_model: str,
    log_path: str | None,
) -> web.StreamResponse:
    """直通转发到 Anthropic 原生 API。"""
    base_url = router_info["base_url"].rstrip("/")
    url = f"{base_url}/v1/messages"

    headers = {
        "x-api-key": router_info["api_key"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # 替换请求中的模型名为目标模型
    try:
        data = json.loads(raw_body.decode("utf-8"))
        data["model"] = router_info["model"]
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        body = raw_body

    if log_path:
        log(log_path, f"  -> {url} (anthropic native)")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.post(url, headers=headers, data=body) as resp:
                raw = await resp.read()
                ct = resp.headers.get("content-type", "")

                if log_path:
                    log(log_path, f"  <- {resp.status} {len(raw)}b")

                return web.Response(
                    body=raw, status=resp.status,
                    headers=dict(_CORS, **{"Content-Type": ct}),
                )
    except Exception as e:
        if log_path:
            log(log_path, f"  ERR: {e}")
        return web.json_response(
            {"type": "error", "error": {"message": str(e)}},
            status=502, headers=dict(_CORS),
        )


# ---- 工具函数 ----

def _lookup_route(model_id: str, route_map: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """路由查找: 精确匹配 → 去后缀匹配 → 前缀匹配 → 第一条目

    插件发送的 model ID 可能是完整格式 (claude-opus-4-7-20250820)
    也可能被 strip 成了短名 (claude-opus-4-7)。
    两种都能命中。
    """
    # 1. 精确匹配
    if model_id in route_map:
        return route_map[model_id]

    # 2. 剥离日期后缀后匹配
    stripped = strip_model_suffix(model_id)
    if stripped in route_map:
        return route_map[stripped]

    # 3. 前缀匹配（裸 claude-opus-4-7 匹配 "claude-opus-4-7-20250820"）
    for key, val in route_map.items():
        if key.startswith(stripped):
            return val

    # 4. 回退到第一个路由条目
    keys = list(route_map.keys())
    if keys:
        return route_map[keys[0]]
    return ("", "")


def _forward_headers(request: web.Request) -> dict:
    """构建转发 HTTP 头（剥离 hop-by-hop 头）。"""
    result = {}
    for k, v in request.headers.items():
        if k.lower() not in _HOP:
            result[k] = v
    result["host"] = request.host
    return result


def _msg_preview(messages: list) -> str:
    """提取最后一条消息的前 80 字符用于日志。"""
    if not messages:
        return ""
    m = messages[-1]
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))[:80]
    return str(c)[:80]


def log(log_path: str, msg: str) -> None:
    """写入日志文件。"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode(), flush=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
