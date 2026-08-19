# FX Gateway Proxy

[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)

An OpenAI-compatible high-performance reverse proxy for the **Vercel AI Gateway (FX Promotional Pool)**.

Enables you to use free top-tier models like **GLM 5.2 / GLM 5.2 Fast (1M context)** with any standard OpenAI-compatible tool or coding agent (such as **Pi**, **Cursor**, **Cline**, **Aider**, **Claude Code**, **LibreChat**, and official OpenAI SDKs).

---

## 🎯 Model Availability & Testing Status

| Model ID | Provider | Status | Free Pool (No Card) | Context Window | Max Output | Features |
| :--- | :--- | :---: | :---: | :--- | :--- | :--- |
| `zai/glm-5.2` | Zhipu AI / ZAI | 🟢 Active | ✅ **Free (Zero Cost)** | 1,000,000 | 128,000 | Deep Reasoning, Tool Calling, Vision, Prompt Caching |
| `zai/glm-5.2-fast` | Zhipu AI / ZAI | 🟢 Active | ✅ **Free (Zero Cost)** | 1,000,000 | 128,000 | Ultra Fast, Tool Calling, Vision, Prompt Caching |
| `meta/muse-spark-1.2-contributor` | Meta | 🟡 Standard | ❌ Requires Card on Vercel | 128,000 | 8,192 | Fast Code & Chat |
| `google/gemini-2.5-flash` | Google | 🟡 Standard | ❌ Requires Card on Vercel | 1,000,000 | 8,192 | Multimodal & Fast Reasoning |

> **Note**: Vercel currently grants zero-card free promotional access exclusively to the **ZAI GLM-5.2 series** (`zai/glm-5.2` & `zai/glm-5.2-fast`). Other models route to the standard gateway and require a verified credit card on your Vercel account.

---

## ✨ Features

- 🔄 **Full OpenAI API Compatibility**: Emulates `/v1/chat/completions`, `/v1/models`, and `/health`.
- ⚡ **Bi-directional SSE Streaming**: Real-time token streaming with instant Time-To-First-Token (TTFT).
- 🧠 **Deep Reasoning Support**: Captures reasoning tokens and streams them via standard `reasoning_content` / delta chunks.
- 🛠️ **Multi-turn Tool Calling**: Seamlessly translates OpenAI tool definitions and tool results to Vercel AI SDK v3 protocol.
- ⚡ **Prompt Caching & Usage Alignment**: Accurately tracks `prompt_tokens_details.cached_tokens` (`cacheRead`) and session affinity (`x-session-affinity`) for maximum KV cache hits.
- 🌐 **Full Parameter Passthrough**: Supports `temperature`, `top_p`, `top_k`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`, `response_format` (JSON mode), and distributed tracing headers (`traceparent`, `x-request-id`).
- 🚀 **Production-Grade Engine**:
  - Global `httpx.AsyncClient` keep-alive connection pooling.
  - Early disconnect handling (aborts upstream gateway tasks on client disconnect / `Ctrl+C`).
  - Zero-config credential resolution (reads from incoming Bearer token, `AI_GATEWAY_API_KEY`, or `~/.fx/api-key`).

---

## 🚀 Quick Start

### Method 1: Single-file Execution with `uv` (Recommended)

No installation required if you have [`uv`](https://github.com/astral-sh/uv):

```bash
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

Or clone and run locally:

```bash
git clone https://github.com/Xeron2000/fx-gateway-proxy.git
cd fx-gateway-proxy
uv run fx-gateway-proxy --host 127.0.0.1 --port 18080
```

### Method 2: Docker / Docker Compose

```bash
docker compose up -d
```

### Method 3: Systemd User Service (Linux Background Daemon)

1. Copy the executable script to your local bin:
   ```bash
   cp fx_gateway_proxy/cli.py ~/.local/bin/fx-gateway-proxy.py
   chmod +x ~/.local/bin/fx-gateway-proxy.py
   ```
2. Place the systemd unit file in `~/.config/systemd/user/fx-gateway-proxy.service`:
   ```ini
   [Unit]
   Description=FX Gateway Reverse Proxy for Vercel AI Free Models
   After=network.target

   [Service]
   Type=simple
   ExecStart=%h/.local/bin/uv run --script %h/.local/bin/fx-gateway-proxy.py
   Restart=always
   RestartSec=3
   Environment=PORT=18080
   Environment=HOST=127.0.0.1

   [Install]
   WantedBy=default.target
   ```
3. Enable and start:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now fx-gateway-proxy.service
   ```

---

## ⚙️ Client Integrations

The proxy listens on `http://127.0.0.1:18080/v1` by default.

### 1. `pi` Coding Agent

Add the `vercel-fx` provider to `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "vercel-fx": {
      "baseUrl": "http://127.0.0.1:18080/v1",
      "api": "openai-completions",
      "apiKey": "dummy",
      "compat": {
        "supportsUsageInStreaming": true,
        "sendSessionAffinityHeaders": true,
        "sessionAffinityFormat": "openai"
      },
      "models": [
        {
          "id": "zai/glm-5.2",
          "name": "GLM 5.2 (FX Free)",
          "reasoning": true,
          "thinkingLevelMap": {
            "minimal": null,
            "low": null,
            "medium": null,
            "high": "high",
            "xhigh": "xhigh",
            "max": null
          },
          "cost": {
            "input": 1.1,
            "output": 3.851,
            "cacheRead": 0.275,
            "cacheWrite": 0
          },
          "contextWindow": 1000000,
          "maxTokens": 128000
        },
        {
          "id": "zai/glm-5.2-fast",
          "name": "GLM 5.2 Fast (FX Free)",
          "reasoning": true,
          "thinkingLevelMap": {
            "minimal": null,
            "low": null,
            "medium": null,
            "high": "high",
            "xhigh": "xhigh",
            "max": null
          },
          "cost": {
            "input": 2.1,
            "output": 6.6,
            "cacheRead": 0.21,
            "cacheWrite": 0
          },
          "contextWindow": 1000000,
          "maxTokens": 128000
        }
      ]
    }
  }
}
```

### 2. Cursor / VSCode / Cline / Continue

Configure OpenAI-compatible settings in your editor/extension:
- **Base URL**: `http://127.0.0.1:18080/v1`
- **API Key**: `dummy` (or your `AI_GATEWAY_API_KEY`)
- **Model**: `zai/glm-5.2` or `zai/glm-5.2-fast`

### 3. OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:18080/v1",
    api_key="dummy"
)

response = client.chat.completions.create(
    model="zai/glm-5.2",
    messages=[
        {"role": "user", "content": "Write a Python script to compute Fibonacci numbers."}
    ],
    stream=True
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
```

---

## 🔑 Authentication

The proxy automatically resolves your API key in the following priority order:
1. **Request Authorization Header**: `Bearer <vck_...>`
2. **Environment Variable**: `AI_GATEWAY_API_KEY`
3. **Local File**: `~/.fx/api-key` (default location used by `fx login`)

---

## 🛡️ License

This project is open-sourced under the [MIT License](LICENSE).
