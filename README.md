# claude-office-proxy

**Bidirectional Anthropic/OpenAI protocol translation proxy. Enables Claude for M365 add-ins to use any OpenAI-compatible backend with full tool calling.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **商标声明**: "Claude" 是 Anthropic PBC 的商标。本项目是独立开源项目，与 Anthropic 无关。本项目不包含、不分发任何 Anthropic 专有代码或模型权重。用户自行负责遵守各 API 提供商的服务条款。

---

一个轻量级代理，实现 Anthropic Messages API 与 OpenAI Chat Completions API 的双向协议翻译。适用于想在自己的基础设施（或第三方 API）上运行 Claude for Office 插件的场景——无需额外容器或外部依赖。

## 特性

- **双向协议翻译**：Anthropic Messages API ↔ OpenAI Chat Completions API
- **完整工具调用**：Excel 单元格读写、图表、格式化等全部 13 种操作
- **流式响应**：thinking 思考链实时可见，不白等
- **多提供商路由**：不同模型名映射到不同后端
- **全 M365 兼容**：Excel / PowerPoint / Word / Outlook 共享同一配置
- **零外部依赖**：不需要 jcode / LiteLLM / Docker / Caddy
- **pip 一键安装**：`pip install claude-office-proxy`

## 原理

```
┌──────────────────┐    HTTPS (TLS+mkcert)    ┌─────────────────────┐    OpenAI/Anthropic    ┌──────────────┐
│  Office 加载项    │ ──────────────────────→ │  claude-office-proxy │ ────────────────────→ │  第三方模型    │
│  pivot.claude.ai │ ←────────────────────── │  :8766               │ ←──────────────────── │  API          │
└──────────────────┘  SSE (Anthropic 格式)    └─────────────────────┘  JSON (流式实时翻译)    └──────────────┘
```

核心工作：逐 chunk 将 OpenAI SSE 流翻译为 Anthropic SSE 流，包括 `tools[].type: "custom"` ↔ `"function"`、`tool_use` ↔ `tool_calls`、`reasoning_content` → `thinking` 等完整映射。

## 快速开始

### 1. 安装

```bash
# 安装 mkcert（一次性，用于生成系统信任的 TLS 证书）
# Windows: winget install mkcert
# macOS: brew install mkcert
# Linux: apt install mkcert 或 pacman -S mkcert

pip install claude-office-proxy

# 生成证书（一次性）
claude-office-proxy --install-cert
```

### 2. 写配置

复制 `config.yaml.example` → `config.yaml`，填自己的 API key：

```yaml
server:
  port: 8766
  token: kaicode

models:
  - claude-opus-4-7 (Provider A)
  - claude-sonnet-4-6 (Provider B)

providers:
  provider-a:
    type: openai
    base_url: https://api.example.com/v1
    api_key: sk-your-key-here

  provider-b:
    type: openai
    base_url: https://api.another-example.com/v1
    api_key: sk-your-key-here

routing:
  "claude-opus-4-7 (Provider A)":
    provider: provider-a
    model: your-model-name
  "claude-sonnet-4-6 (Provider B)":
    provider: provider-b
    model: your-model-name
```

### 3. 启动

```bash
claude-office-proxy --config config.yaml
```

### 4. 连接 Office

打开 Excel / PPT / Word → Claude 插件 → **Enterprise gateway (Beta)**：

| 字段 | 值 |
|------|-----|
| Gateway URL | `https://127.0.0.1:8766` |
| Token | `kaicode`（或你配置的 token） |

**所有 Office 应用共享同一配置，配一次即可。**

## 配置详解

### 提供商

```yaml
providers:
  my-provider:
    type: openai          # openai: 走协议翻译；anthropic: 直通转发
    base_url: https://api.example.com/v1
    api_key: sk-xxx
```

- `type: openai` — 协议翻译路径（适合绝大多数 OpenAI 兼容的第三方 API）
- `type: anthropic` — 直通转发（适合已支持 Anthropic 原生协议的代理/路由服务）

### 模型路由

```yaml
models:                          # 插件下拉列表显示的名字
  - claude-opus-4-7 (My Model)   # ⚠️ 必须以 claude- 开头，否则插件不显示

routing:
  "claude-opus-4-7 (My Model)":  # 必须与 models 列表完全一致
    provider: my-provider
    model: real-model-name       # 该 provider 的模型 ID
```

### 多后端示例：Ollama 本地模型

```yaml
providers:
  ollama:
    type: openai
    base_url: http://localhost:11434/v1
    api_key: ollama

routing:
  "claude-opus-4-7 (Ollama qwen)":
    provider: ollama
    model: qwen2.5:14b
```

工具调用效果取决于模型自身能力。

## 兼容性

代理与任何兼容 OpenAI Chat Completions API 或 Anthropic Messages API 的后端兼容。工具调用效果取决于模型自身能力——响应指令越准确的模型，Office 操作成功率越高。

已实际测试的后端包括 DeepSeek、小米 Mimo、第三方中转站、Ollama 本地模型等。完整的端到端测试报告见项目文档。

## 版本历史

### v0.2.0 (current)

- 重写流式 `_stream_translate()` — 逐 block 严格顺序打开/关闭 Anthropic SSE
- 修复 `content_block_start` index 不一致 → SDK 丢弃整个 block（thinking 消失根因）
- 修复 `anth_to_openai` 丢弃对话历史中的 assistant `tool_use`（第二轮 tool_result 被拒绝根因）
- 添加 `aiohttp drain()` 强制刷新，解决小 chunk 缓冲区不刷问题

### v0.1.0

- Anthropic ↔ OpenAI 双向协议翻译
- 工具调用完整支持
- 流式 SSE 实时转发（thinking 实时可见）
- 多提供商路由
- mkcert TLS 证书管理
- PPT / Word / Outlook 全兼容
- 修复 `content_block_start` 缺 `input:{}`（工具调用静默丢弃）
- 修复 `tool_choice: {"type": "auto"}` 未处理
- 修复 stream=false 导致 thinking 不实时
- 修复 content=null 导致的 NoneType crash
- 修复大上下文超时

## License

MIT — 随便用、随便改、随便商用。
