# FX Gateway Proxy

[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)

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
If you have `fx` installed, simply run:
```bash
fx login
```
This automatically authenticates and stores your API key at `~/.fx/api-key`. **The proxy will automatically detect and read this file.**

### Option B: Via Vercel Web Dashboard
1. Open [Vercel AI Gateway API Keys](https://vercel.com/~/ai-gateway/api-keys).
2. Create a new API Key (`vck_...`).
3. Set it as an environment variable:
   ```bash
   export AI_GATEWAY_API_KEY="vck_your_api_key_here"
   ```

---

## 🚀 Step 2: Start the Reverse Proxy

The proxy listens on `http://127.0.0.1:18080/v1` by default.

### Method 1: Single-file Execution with `uv` (Recommended)

```bash
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

Or clone and run locally:
```bash
git clone https://github.com/Xeron2000/fx-gateway-proxy.git
cd fx-gateway-proxy
uv run fx-gateway-proxy
```

### Method 2: Systemd User Service (Background Daemon)

```bash
# 1. Copy script
cp fx_gateway_proxy/cli.py ~/.local/bin/fx-gateway-proxy.py
chmod +x ~/.local/bin/fx-gateway-proxy.py

# 2. Add systemd service to ~/.config/systemd/user/fx-gateway-proxy.service
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

Edit `~/.pi/agent/models.json` and add the `vercel-fx` provider:

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

> **API Key in `models.json`**:
> - `"apiKey": "dummy"`: When set to `"dummy"` or `"placeholder"`, the proxy automatically falls back to your local `~/.fx/api-key` or `AI_GATEWAY_API_KEY` environment variable.
> - Alternatively, you can directly set `"apiKey": "vck_your_key"` or `"apiKey": "AI_GATEWAY_API_KEY"` to pass an explicit key.

**Launch Pi with GLM 5.2**:
```bash
pi --provider vercel-fx --model zai/glm-5.2
```
Or switch models inside Pi using `/model zai/glm-5.2`.

---

### 2. Cursor / VSCode / Cline / Continue

Configure OpenAI-compatible settings in your editor:
- **Base URL**: `http://127.0.0.1:18080/v1`
- **API Key**: `dummy` (or your `vck_...` key)
- **Model**: `zai/glm-5.2` or `zai/glm-5.2-fast`

---

### 3. OpenAI Python SDK

```python
from openai import OpenAI

# The proxy will use ~/.fx/api-key or AI_GATEWAY_API_KEY when api_key is "dummy"
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

The proxy resolves credentials in the following order:
1. `Authorization: Bearer <vck_...>` (if a real `vck_` key is passed)
2. `AI_GATEWAY_API_KEY` environment variable
3. Local file: `~/.fx/api-key` (from `fx login`)

---

## 🛡️ License

[MIT](LICENSE)
