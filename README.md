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

An OpenAI-compatible reverse proxy for Vercel AI Gateway's free promotional pool (**GLM 5.2 / GLM 5.2 Fast**), featuring **Adaptive Multi-Key Routing, Learned Capacity Ceilings, and Automatic Cooldown Rotation**.

Works out-of-the-box with **Pi**, **Cursor**, **Cline**, **Aider**, **Claude Code**, and any standard OpenAI SDK.

---

## 🎯 Supported Models

| Model ID | Provider | Context Window | Max Output | Features |
| :--- | :--- | :---: | :---: | :--- |
| `zai/glm-5.2` | ZAI / Zhipu | 1,000,000 | 128,000 | Reasoning, Tool Calling, Vision, Prompt Caching |
| `zai/glm-5.2-fast` | ZAI / Zhipu | 1,000,000 | 128,000 | Fast Inference, Tool Calling, Vision, Prompt Caching |

---

## ✨ Features

- 🔄 **Adaptive Multi-Key Routing (`KeyPool`)**:
  - Sliding 60s window tracks per-key request & token loads.
  - Learns true RPM/TPM ceilings from 429 snapshots (0.8x) and high-load successes (1.1x).
  - Weighted selection: low-load keys win with anti-thundering jitter.
  - Automatic 429 exponential backoff cooldown (30s base, 300s cap) with seamless key rotation.
- 📊 **Live Stats Endpoint**: `GET /v1/stats` returns real-time masked metrics, loads, and cooldown states.
- 🛡️ **Edge-Case Hardened**: Sanitizes empty user messages and structured multimodal payloads to prevent upstream validation errors.

---

## 🔑 Step 1: Supply Your API Keys

Keys can be configured in any of the following ways:

### Option A: `fx login` (Single key)
```bash
fx login
```
Saves your key to `~/.fx/api-key`.

### Option B: Multi-Key Environment or File (Recommended)
Add one key per line in `~/.fx/api-key`, or set via environment variable:
```bash
# Multi-key (comma or newline separated)
export AI_GATEWAY_API_KEYS="vck_key1...,vck_key2...,vck_key3..."

# Single key
export AI_GATEWAY_API_KEY="vck_key1..."
```

---

## 🚀 Step 2: Run the Proxy

Default endpoint: `http://127.0.0.1:18080/v1`

### Method 1: Direct via `uvx` / `uv run` (Zero install)

```bash
# Option A: Run directly from GitHub via uvx
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# Option B: Run standalone single-file script
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

### Method 2: Systemd User Service

```bash
cp fx-gateway-proxy.py ~/.local/bin/fx-gateway-proxy.py
chmod +x ~/.local/bin/fx-gateway-proxy.py

mkdir -p ~/.config/systemd/user
cat << 'SERVICE' > ~/.config/systemd/user/fx-gateway-proxy.service
[Unit]
Description=FX Gateway Reverse Proxy
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/uv run --script %h/.local/bin/fx-gateway-proxy.py
Restart=always
RestartSec=3
Environment=PORT=18080
Environment=HOST=127.0.0.1

[Install]
WantedBy=default.target
SERVICE

systemctl --user daemon-reload
systemctl --user enable --now fx-gateway-proxy.service
```

---

## ⚙️ Step 3: Configure Your Clients

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
          "contextWindow": 1000000,
          "maxTokens": 128000
        },
        {
          "id": "zai/glm-5.2-fast",
          "name": "GLM 5.2 Fast (FX Free)",
          "reasoning": true,
          "contextWindow": 1000000,
          "maxTokens": 128000
        }
      ]
    }
  }
}
```

---

## 🛣️ Gateway Pathways: fx vs eve In-Depth

Vercel AI Gateway provides two promotional entry points for the free GLM 5.2 pool. Both route to the **exact same Blackbox `system` credential pool** ($0 cost, no balance deductions).

| Dimension | **fx Pathway** (Default) | **eve Pathway** |
| --- | --- | --- |
| Endpoint | `/v3/ai/language-model` | `/v4/ai/language-model` |
| HTTP `User-Agent` | `fx/0.0.3` | `eve/0.39.1 ai-sdk-agent/tool-loop ...` |
| `body.headers` | `{user-agent, x-title}` | `{user-agent, x-title}` |
| Additional Header | `HTTP-Referer: github.com/vercel-labs/fx` | `ai-gateway-auth-method: api-key` |
| Routing Destination | Blackbox (`credentialType: system`) | Blackbox (`credentialType: system`) |
| Billing | $0 | $0 |

### The Exact Trigger Switches for Free Promotion

The promotion is **not** tied to IP or API key tier. The gateway matches promo requests based on **two mandatory flags**:
1. HTTP `User-Agent` starts with `fx/` or `eve/` (case-sensitive)
2. The JSON request body `headers` object contains both `user-agent` and `x-title`.

---

## 🚦 Rate Limits & Adaptive Retries

The free tier enforces rate limits per account and per model. 

### Built-in Exponential Backoff & Key Rotation

The proxy automatically retries transient failures across `429, 500, 502, 503, 504`:

```python
MAX_RETRIES  = 5                # Environment variable FX_MAX_RETRIES
BASE_DELAY   = 0.8s            # Environment variable FX_BASE_DELAY
MAX_DELAY    = 20.0s           # Environment variable FX_MAX_DELAY
delay(attempt) = min(BASE_DELAY * 2**attempt, MAX_DELAY)
# Backoff sequence: 0.8 → 1.6 → 3.2 → 6.4 → 12.8 (capped at 20s)
```

In multi-key setups, a 429 key is placed in exponential backoff cooldown (30s~300s) and the proxy rotates instantly to the next available key.

---

## 📊 Live Metrics Monitoring

```bash
curl http://127.0.0.1:18080/v1/stats
```

Example response:
```json
{
  "keys": [
    {
      "key": "vck_66t...NkTR",
      "status": "active",
      "success": 28,
      "failed_429": 0,
      "load": 0.12,
      "est_rpm": 60,
      "est_tpm": 20000,
      "last_latency_ms": 680
    }
  ],
  "total": 1
}
```

---

## 🤝 Community Recommendation

Special thanks to **[LINUX DO](https://linux.do)**:
- 💡 **Cutting-Edge Tech**: A thriving community exploring state-of-the-art AI, developer tools, protocol reverse engineering, and hands-on coding insights.
- 🌟 **Sincere & Collaborative**: A community built on genuine knowledge sharing and collaboration.

---

## 🛡️ License

[MIT](LICENSE)
