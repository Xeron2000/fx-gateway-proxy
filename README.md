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
1. Go to [Vercel AI Gateway](https://vercel.com/ai-gateway).
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

## 🛣️ Gateway Channels: fx vs eve

Vercel AI Gateway exposes two promotional entry points for the free GLM 5.2 pool. Both route to the **same Blackbox `system` credential pool** (cost: $0, no balance deduction) under the hood — they differ only in endpoint version, User-Agent, and identifying headers.

| Dimension | **fx channel** (default) | **eve channel** |
| --- | --- | --- |
| Endpoint | `/v3/ai/language-model` | `/v4/ai/language-model` |
| HTTP `User-Agent` | `fx/0.0.3` | `eve/0.39.1 ai-sdk-agent/tool-loop ...` |
| `body.headers` | `{user-agent, x-title}` | `{user-agent, x-title}` |
| Extra header | `HTTP-Referer: github.com/vercel-labs/fx` | `ai-gateway-auth-method: api-key` |
| API key | same `vck_...` | same `vck_...` |
| Routed provider | Blackbox (`credentialType: system`) | Blackbox (`credentialType: system`) |
| Cost | $0 | $0 |

### What actually triggers the free pool

The promo is **not** keyed on the key, the IP, or the endpoint version. The gateway recognizes a promo request by **two markers that must both be present**:

1. HTTP `User-Agent` starting with `fx/` or `eve/` (case-sensitive)
2. The request body's `headers` object containing **both** `user-agent` **and** `x-title`

Drop either marker and the gateway returns `customer_verification_required` (asks for a credit card on file). The exact UA value does not matter as long as the prefix matches.

### Do you need the eve channel?

**No.** This proxy uses the fx channel by default and it is equivalent to eve for the free promo:

- Same key, same Blackbox `system` credential, same $0 cost, same balance behavior
- Cross-verified: `fx key + eve UA` ✓, `eve key + fx UA` ✓ — all reach the free pool
- The free tier **rate limit** is per-account-per-model, so switching channels does **not** bypass throttling

An eve-channel mode is **not** implemented because it would add complexity for zero benefit. Override the channel markers via env if you ever need it:

```bash
FX_USER_AGENT="eve/0.39.1" UPSTREAM_URL=https://ai-gateway.vercel.sh/v4/ai/language-model uv run fx-gateway-proxy.py
```

---

## 🚦 Rate Limiting & Retry

The free tier enforces a **per-account, per-model** rate limit. It is **not** per-IP and **not** per-key — switching IPs or keys within the same Vercel team account will not reset it. Exceeding the limit returns `429 rate_limit_exceeded` with `providerAttemptCount: 0` (the request is rejected at the gateway layer, never reaching the provider).

### Observed behavior

- Serial requests: almost never throttled
- Moderate concurrency (≈20 parallel): a few 429s, most succeed
- High concurrency (≈30+ parallel): widespread 429s
- Recovery window: short — a few tens of seconds

So the limit is a burst/concurrency ceiling, not a hard RPM cap. Normal single-agent usage rarely hits it.

### Built-in exponential backoff

The proxy retries transient failures automatically — both streaming and non-streaming paths. Retriable status codes: `429, 500, 502, 503, 504`, plus network/connection exceptions.

```python
MAX_RETRIES  = 5                # default; env FX_MAX_RETRIES
BASE_DELAY   = 0.8s            # env FX_BASE_DELAY
MAX_DELAY    = 20.0s           # env FX_MAX_DELAY
delay(attempt) = min(BASE_DELAY * 2**attempt, MAX_DELAY)
# backoff series: 0.8 → 1.6 → 3.2 → 6.4 → 12.8 (capped at 20s)
```

After retries are exhausted the proxy surfaces the last error to the client (stream: an error chunk + `[DONE]`; non-stream: an HTTPException / JSONResponse with the upstream status).

### Cannot bypass — only mitigate

- The limit is account-level at the gateway; there is no client-side circumvention
- Buying AI Gateway Credits upgrades to the paid tier (higher limits, but loses the free quota)
- For bursty workloads, keep client-side concurrency modest (this proxy does not add a concurrency limiter by design — the agent loop is naturally serial)

---

## 🤝 Community & Recommendation

Special thanks and strong recommendation for **[LINUX DO](https://linux.do)** () — an active, sincere, and innovative community for geeks, AI explorers, and software developers.

---

## 🛡️ License

[MIT](LICENSE)
