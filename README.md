# FX Gateway Proxy

[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)

OpenAI-compatible reverse proxy for Vercel AI Gateway's free **GLM 5.2** promotional pool.

Works out of the box with **Pi**, **Cursor**, **Cline**, **Aider**, **Claude Code**, and standard OpenAI SDKs.

---

## 🎯 Models

| Model ID | Provider | Context | Output | Features |
| :--- | :--- | :---: | :---: | :--- |
| `zai/glm-5.2` | Zhipu AI / ZAI | 1,000,000 | 128,000 | Deep Reasoning, Tool Calling, Vision, Prompt Caching |
| `zai/glm-5.2-fast` | Zhipu AI / ZAI | 1,000,000 | 128,000 | Ultra Fast, Tool Calling, Vision, Prompt Caching |

---

## 🚀 Quick Start

### 1. Run with `uv`

```bash
# Run standalone script directly
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

Or clone and run:

```bash
git clone https://github.com/Xeron2000/fx-gateway-proxy.git
cd fx-gateway-proxy
uv run fx-gateway-proxy
```

### 2. Docker

```bash
docker compose up -d
```

---

## ⚙️ Integrations

Proxy endpoint: `http://127.0.0.1:18080/v1`

### Pi Coding Agent

Add to `~/.pi/agent/models.json`:

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
          "cost": { "input": 1.1, "output": 3.851, "cacheRead": 0.275, "cacheWrite": 0 },
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
          "cost": { "input": 2.1, "output": 6.6, "cacheRead": 0.21, "cacheWrite": 0 },
          "contextWindow": 1000000,
          "maxTokens": 128000
        }
      ]
    }
  }
}
```

```bash
pi --provider vercel-fx --model zai/glm-5.2
```

### Cursor / Cline / OpenAI SDK

- **Base URL**: `http://127.0.0.1:18080/v1`
- **API Key**: `dummy` (auto-reads from `~/.fx/api-key` or `AI_GATEWAY_API_KEY`)
- **Model**: `zai/glm-5.2` or `zai/glm-5.2-fast`

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18080/v1", api_key="dummy")
response = client.chat.completions.create(
    model="zai/glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

---

## 🔑 Authentication

Key resolution priority:
1. `Authorization: Bearer <key>` header
2. `AI_GATEWAY_API_KEY` environment variable
3. `~/.fx/api-key` (from `fx login`)

---

## 🛡️ License

[MIT](LICENSE)
