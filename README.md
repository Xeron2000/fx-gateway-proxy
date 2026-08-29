# FX Gateway Proxy

<div align="center">

[![LINUX DO](https://img.shields.io/badge/Community-LINUX%20DO-blue?style=flat&logo=discourse&logoColor=white)](https://linux.do)
[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![Docker Image](https://img.shields.io/badge/docker-GHCR-blue.svg?logo=docker&logoColor=white)](https://github.com/Xeron2000/fx-gateway-proxy/pkgs/container/fx-gateway-proxy)
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
| `zai/glm-5.2` | Blackbox AI | 192,000¹ | 128,000 | Reasoning, Tool Calling, Vision, Prompt Caching |
| `zai/glm-5.2-fast` | Blackbox AI | 192,000¹ | 128,000 | Fast Inference, Tool Calling, Vision, Prompt Caching |

> ¹ **Context Window Notice**: The free pool via Vercel AI Gateway defaults to ~192k (max 202,752) on the non-`[1m]` variant; Vercel catalog reports 1,000,000 but free credits cannot access `zai/glm-5.2[1m]` (403 `customer_verification_required`). 1M requires paid credits. Verified: 192k OK, 195k+ → 413 `Request Entity Too Large`.

### 🧠 Thinking Levels (Reasoning Effort)

The proxy includes automatic thinking effort normalization. You can pass reasoning levels via OpenAI `reasoning_effort`, Pi `thinkingLevel`, or Anthropic `thinking`, and the proxy smoothly normalizes them to upstream Vercel tiers:

| Client Level (`reasoning_effort` / `thinkingLevel`) | Upstream Vercel Tier | Description |
| :--- | :--- | :--- |
| `off` | `none` | Base reasoning |
| `minimal` / `low` / `medium` / `auto` | `auto` | ~200–400 reasoning tokens |
| `high` | `high` | Deep reasoning (~500+ tokens) |
| `xhigh` / `max` | `xhigh` | Full-budget maximum reasoning (Vercel's highest tier) |

---

## ✨ Features

- 🔄 **Adaptive Multi-Key Routing (`KeyPool`)**:
  - Sliding 60s window tracks per-key request & token loads.
  - Learns true RPM/TPM ceilings from 429 snapshots (0.8x) and high-load successes (1.1x).
  - Weighted selection: low-load keys win with anti-thundering jitter.
  - Automatic 429 exponential backoff cooldown (30s base, 300s cap) with seamless key rotation.
- 🔌 **Triple Protocol Support**: `POST /v1/chat/completions` (OpenAI Chat), `POST /v1/responses` (OpenAI Responses), `POST /v1/messages` (Anthropic Messages) + `POST /v1/messages/count_tokens` — all share the same adaptive `KeyPool`, model aliasing (`claude-*`/`gpt-*` → `zai/glm-5.2(-fast)`) and `x-api-key`/`Authorization` auth.
- 📊 **Live Stats Endpoint**: `GET /v1/stats` returns real-time masked metrics, loads, and cooldown states.
- 🛡️ **Edge-Case Hardened**: Sanitizes empty user messages and structured multimodal payloads to prevent upstream validation errors.
- 🌐 **Optional outbound proxy (`FX_PROXY`)**: local HTTP proxy for upstream Vercel calls. Default is direct (leave unset in Docker). Separate from `PROXY_API_KEY`.

---

## 🔑 Step 1: Obtain & Configure Your API Keys

Create your API Key (format: `vck_...`) directly from the **[Vercel AI Gateway Console](https://vercel.com/ai-gateway)**.

Keys can be configured into the proxy in any of the following ways:

### Option A: Environment Variables (Recommended)
```bash
# Multi-key (comma or newline separated, linearly scales rate limits)
export AI_GATEWAY_API_KEYS="vck_key1...,vck_key2...,vck_key3..."

# Single key
export AI_GATEWAY_API_KEY="vck_key1..."
```

### Option B: Local Credential File (One key per line)
Write your key(s) into `~/.fx/api-key` (one per line). The proxy automatically detects and reads this file at runtime:
```bash
mkdir -p ~/.fx
echo "vck_your_key" >> ~/.fx/api-key
```

### 🔒 Option C: Public Deployment Access Protection (`PROXY_API_KEY`)
When deploying on public networks (e.g. Hugging Face Spaces, VPS, Zeabur, Railway), set `PROXY_API_KEY` or `PROXY_API_KEYS` to require authentication and prevent unauthorized quota consumption:
```bash
# Single access key
export PROXY_API_KEY="your-custom-password"

# Multiple access keys (comma-separated)
export PROXY_API_KEYS="token1,token2,token3"
```
When configured, clients must pass this token via `Authorization: Bearer <token>` or `x-api-key: <token>`. The proxy validates the client token before routing the request to the upstream `KeyPool`.

### Option D: Outbound HTTP Proxy (`FX_PROXY`)
If this machine cannot reach `ai-gateway.vercel.sh` directly, set `FX_PROXY` or pass `--proxy`. Docker and public hosts should leave it unset.

```bash
export FX_PROXY="http://127.0.0.1:7890"
# or
fx-gateway-proxy --proxy http://127.0.0.1:7890
```

`FX_PROXY=none` forces direct. This is not `PROXY_API_KEY` (that one is the client access password).

---

## 🚀 Step 2: Run the Proxy

Default endpoint: `http://127.0.0.1:18080/v1`

### 🏆 Method 1: Docker Container (Recommended, Prebuilt Multi-Arch GHCR Image)

Zero local setup required, supports `linux/amd64` and `linux/arm64`:

#### Option A: Direct Docker Run (Reading local ~/.fx/api-key)
```bash
docker run -d \
  --name fx-gateway-proxy \
  --restart unless-stopped \
  -p 18080:18080 \
  -v ~/.fx:/root/.fx:ro \
  ghcr.io/xeron2000/fx-gateway-proxy:latest
```

#### Option B: Inject Multi-Key & Proxy Password via Environment
```bash
docker run -d \
  --name fx-gateway-proxy \
  --restart unless-stopped \
  -p 18080:18080 \
  -e AI_GATEWAY_API_KEYS="vck_key1,vck_key2,vck_key3" \
  -e PROXY_API_KEY="your-custom-password" \
  ghcr.io/xeron2000/fx-gateway-proxy:latest
```

#### Option C: Docker Compose
Create `docker-compose.yml`:
```yaml
services:
  fx-gateway-proxy:
    image: ghcr.io/xeron2000/fx-gateway-proxy:latest
    container_name: fx-gateway-proxy
    restart: unless-stopped
    ports:
      - "18080:18080"
    environment:
      - HOST=0.0.0.0
      - PORT=18080
      - AI_GATEWAY_API_KEYS=${AI_GATEWAY_API_KEYS:-}
      - AI_GATEWAY_API_KEY=${AI_GATEWAY_API_KEY:-}
      - PROXY_API_KEY=${PROXY_API_KEY:-}
      - PROXY_API_KEYS=${PROXY_API_KEYS:-}
    volumes:
      - ~/.fx:/root/.fx:ro
```
Run:
```bash
docker compose up -d
```


---

### Method 2: Direct via `uvx` / `uv run` (Zero install)

```bash
# Option A: Run directly from GitHub via uvx
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# Option B: Run standalone single-file script
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

---

### Method 3: Systemd User Service

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
          "contextWindow": 192000,
          "maxTokens": 128000
        },
        {
          "id": "zai/glm-5.2-fast",
          "name": "GLM 5.2 Fast (FX Free)",
          "reasoning": true,
          "contextWindow": 192000,
          "maxTokens": 128000
        }
      ]
    }
  }
}
```

---

## 🔌 Triple Protocol Examples

All three protocols are proxied to the same Vercel pool:

```bash
# OpenAI Chat
curl http://127.0.0.1:18080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"zai/glm-5.2","messages":[{"role":"user","content":"hi"}]}'

# OpenAI Responses
curl http://127.0.0.1:18080/v1/responses -H "Content-Type: application/json" \
  -d '{"model":"zai/glm-5.2","input":"hi","instructions":"You are helpful"}'

# Anthropic Messages (x-api-key or Authorization)
curl http://127.0.0.1:18080/v1/messages -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -H "x-api-key: dummy" \
  -d '{"model":"zai/glm-5.2","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
```

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
