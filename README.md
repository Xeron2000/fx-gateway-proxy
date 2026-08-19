# FX Gateway Proxy

<div align="center">

[![LINUX DO](https://img.shields.io/badge/Community-LINUX%20DO-blue?style=flat&logo=discourse&logoColor=white)](https://linux.do)
[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)

**[English](README.md)** | **[简体中文](README_CN.md)**

> 🤝 **Friendly Links**: [LINUX DO Community](https://linux.do)

</div>

---

OpenAI-compatible reverse proxy for Vercel AI Gateway's promotional free pool (**GLM 5.2 / GLM 5.2 Fast**).

Works out of the box with **Pi**, **Cursor**, **Cline**, **Aider**, **Claude Code**, and standard OpenAI SDKs.

---

## 🎯 Models

| Model ID | Provider | Context | Output | Features |
| :--- | :--- | :---: | :---: | :--- |
| `zai/glm-5.2` | Zhipu AI / ZAI | 1,000,000 | 128,000 | Deep Reasoning, Tool Calling, Vision, Prompt Caching |
| `zai/glm-5.2-fast` | Zhipu AI / ZAI | 1,000,000 | 128,000 | Ultra Fast, Tool Calling, Vision, Prompt Caching |

---

## 🔑 Step 1: Obtain Vercel AI Gateway API Key

Choose **either** method:

### Option A: Via `fx` CLI (Recommended & Automated)
If you have `fx` installed, run:
```bash
fx login
```
This stores your API key at `~/.fx/api-key`. **The proxy will automatically detect and read this file.**

### Option B: Via Vercel Web Dashboard
1. Go to [Vercel AI Gateway API Keys](https://vercel.com/~/ai-gateway/api-keys).
2. Create an API Key (`vck_...`).
3. Set it as an environment variable:
   ```bash
   export AI_GATEWAY_API_KEY="vck_your_api_key_here"
   ```

---

## 🚀 Step 2: Start the Reverse Proxy

Default endpoint: `http://127.0.0.1:18080/v1`

### Method 1: Remote Execution with `uv` / `uvx` (Zero Install)

```bash
# Option A: Run directly from GitHub repo via uvx
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# Option B: Run standalone script from raw URL
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

### Method 2: Systemd User Service (Background Daemon)

```bash
# 1. Copy script
cp fx_gateway_proxy/cli.py ~/.local/bin/fx-gateway-proxy.py
chmod +x ~/.local/bin/fx-gateway-proxy.py

# 2. Add systemd service
mkdir -p ~/.config/systemd/user
cat << 'SERVICE' > ~/.config/systemd/user/fx-gateway-proxy.service
[Unit]
Description=FX Gateway Reverse Proxy
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
SERVICE

# 3. Enable & start
systemctl --user daemon-reload
systemctl --user enable --now fx-gateway-proxy.service
```

### Method 3: Docker Compose

```bash
docker compose up -d
```

---

## ⚙️ Step 3: Configure Clients

### 1. `pi` Coding Agent

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

> **Note on `apiKey`**:
> - `"apiKey": "dummy"`: The proxy automatically resolves the real key from `~/.fx/api-key` or `AI_GATEWAY_API_KEY`.
> - You can also pass `"apiKey": "vck_..."` explicitly.

**Launch Pi**:
```bash
pi --provider vercel-fx --model zai/glm-5.2
```

---

### 2. Cursor / VSCode / Cline / Continue

- **Base URL**: `http://127.0.0.1:18080/v1`
- **API Key**: `dummy` (or `vck_...`)
- **Model**: `zai/glm-5.2` or `zai/glm-5.2-fast`

---

### 3. OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18080/v1", api_key="dummy")

response = client.chat.completions.create(
    model="zai/glm-5.2",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

---

## 🔒 Authentication Resolution Hierarchy

1. `Authorization: Bearer <vck_...>` header
2. `AI_GATEWAY_API_KEY` environment variable
3. Local credential file: `~/.fx/api-key` (generated by `fx login`)

---

## 🤝 Community & Recommendation

Special thanks and strong recommendation for **[LINUX DO](https://linux.do)** () — an active, sincere, and innovative community for geeks, AI explorers, and software developers.

---

## 🛡️ License

[MIT](LICENSE)
