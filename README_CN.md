# FX Gateway Proxy

<div align="center">

[![LINUX DO](https://img.shields.io/badge/社区-LINUX%20DO-blue?style=flat&logo=discourse&logoColor=white)](https://linux.do)
[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
[![Docker Image](https://img.shields.io/badge/docker-GHCR-blue.svg?logo=docker&logoColor=white)](https://github.com/Xeron2000/fx-gateway-proxy/pkgs/container/fx-gateway-proxy)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/badge/managed%20by-uv-261230.svg)](https://github.com/astral-sh/uv)

**[English](README.md)** | **[简体中文](README_CN.md)**

> 🤝 **友情链接**: [LINUX DO 论坛](https://linux.do)

</div>

---

专为 Vercel AI Gateway 免费促销池（**GLM 5.2 / GLM 5.2 Fast**）打造的 OpenAI 兼容协议反向代理服务器，内置**多 Key 自适应负载路由与智能冷却退避**。

开箱即用，原生支持 **Pi**、**Cursor**、**Cline**、**Aider**、**Claude Code** 及任意标准 OpenAI SDK。

---

## 🎯 模型支持

| 模型 ID | 提供方 | 上下文窗口 | 单次最大输出 | 特性 |
| :--- | :--- | :---: | :---: | :--- |
| `zai/glm-5.2` | Blackbox AI | 1,000,000 | 128,000 | 深度思考 (Reasoning)、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |
| `zai/glm-5.2-fast` | Blackbox AI | 1,000,000 | 128,000 | 极速响应、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |

---

## ✨ 核心特性

- 🔄 **自适应多 Key 智能路由（Adaptive KeyPool）**：
  - 支持配置多把 Key，基于 60s 滑动窗口自动追踪各 Key 的 Request/Token 负载；
  - 动态学习每个 Key 的有效 RPM/TPM 上限，加权优先调度低负载 Key 并带抖动防惊群；
  - 遇到 429 速率限制时自动加入指数冷却退避（基准 30s，封顶 300s），并无感秒级轮换下一把可用 Key。
- 📊 **实时指标监控**：提供 `GET /v1/stats` 端点，脱敏展示各 Key 实时负载率、成功数、429 触发数及估算上限。
- 🛡️ **极限边界健壮性**：
  - 自动清洗客户端发送的空消息与空白字符，彻底避免 Vercel 400 校验错误；
  - 严格兼容 OpenAI 非流式响应规范，支持多轮工具调用闭环与 Prompt Caching 数据上报。

---

## 🔑 第一步：配置 API Key

支持以下**任意一种**配置方式（单 Key 或多 Key 自动轮换）：

### 方式 A：通过 `fx` CLI 登录（单 Key / 自动生成）
如果本地安装了 `fx`，直接在终端执行：
```bash
fx login
```
登录成功后密钥保存在 `~/.fx/api-key`。**反代服务启动时会自动识别并读取该文件。**

### 方式 B：配置多 Key（推荐，突破单 Key 限流）
可在 `~/.fx/api-key` 中每行填入一把 Key，或通过环境变量注入（逗号/换行分隔）：
```bash
# 多 Key 环境变量（多账号汇聚）
export AI_GATEWAY_API_KEYS="vck_key1...,vck_key2...,vck_key3..."

# 单 Key 环境变量
export AI_GATEWAY_API_KEY="vck_key1..."
```

---

## 🚀 第二步：启动反向代理服务

服务默认监听端口：`http://127.0.0.1:18080/v1`

### 🏆 方法 1：Docker 容器部署（首选推荐，直接拉取 GHCR 预编译镜像）

无需本地安装 Python / uv 环境，支持 `linux/amd64` 与 `linux/arm64`（Apple Silicon / VPS）：

#### 选项 A：单命令行直接运行（读取本地 ~/.fx/api-key）
```bash
docker run -d \
  --name fx-gateway-proxy \
  --restart unless-stopped \
  -p 18080:18080 \
  -v ~/.fx:/root/.fx:ro \
  ghcr.io/xeron2000/fx-gateway-proxy:latest
```

#### 选项 B：环境变量注入多 Key 运行
```bash
docker run -d \
  --name fx-gateway-proxy \
  --restart unless-stopped \
  -p 18080:18080 \
  -e AI_GATEWAY_API_KEYS="vck_key1,vck_key2,vck_key3" \
  ghcr.io/xeron2000/fx-gateway-proxy:latest
```

#### 选项 C：通过 Docker Compose 启动
创建 `docker-compose.yml`：
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
    volumes:
      - ~/.fx:/root/.fx:ro
```
后台启动：
```bash
docker compose up -d
```

---

### 方法 2：通过 `uv` / `uvx` 远端直接运行（免安装环境）

```bash
# 方式 A：通过 uvx 从 GitHub 仓库一键启动
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# 方式 B：通过 uv run 运行远端单文件脚本
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

---

### 方法 3：Systemd 用户服务（Linux 后台常驻守护）

```bash
# 1. 复制单文件脚本
cp fx-gateway-proxy.py ~/.local/bin/fx-gateway-proxy.py
chmod +x ~/.local/bin/fx-gateway-proxy.py

# 2. 配置 Systemd 服务
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

# 3. 启用并启动服务
systemctl --user daemon-reload
systemctl --user enable --now fx-gateway-proxy.service
```

---

## ⚙️ 第三步：配置客户端接入

### 1. `pi` Coding Agent

编辑 `~/.pi/agent/models.json`，在 `providers` 中添加 `vercel-fx`：

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

> **关于 `apiKey` 的说明**：
> - 填 `"apiKey": "dummy"`：反代会自动从 `~/.fx/api-key` 或 `AI_GATEWAY_API_KEYS` 环境变量中提取真实密钥。
> - 亦可直接填写显式密钥 `"apiKey": "vck_..."`。

**启动 Pi 体验**：
```bash
pi --provider vercel-fx --model zai/glm-5.2
```

---

### 2. Cursor / VSCode / Cline / Continue

在编辑器设置中配置 OpenAI 兼容接口：
- **Base URL**: `http://127.0.0.1:18080/v1`
- **API Key**: `dummy`（或你的 `vck_...`）
- **Model**: `zai/glm-5.2` 或 `zai/glm-5.2-fast`

---

### 3. Python OpenAI SDK 调用示例

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:18080/v1", api_key="dummy")

response = client.chat.completions.create(
    model="zai/glm-5.2",
    messages=[{"role": "user", "content": "你好，请介绍一下你自己"}],
    stream=True
)

for chunk in response:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

---

## 🔒 密钥自动解析优先级

1. 请求头：`Authorization: Bearer <vck_...>`
2. 环境变量：`AI_GATEWAY_API_KEYS`（逗号/换行分隔）或 `AI_GATEWAY_API_KEY`
3. 本地凭证文件：`~/.fx/api-key`（由 `fx login` 自动生成或手动添加多行）

---

## 🛣️ 网关通路：fx 与 eve 深度解析

Vercel AI Gateway 为免费 GLM 5.2 池提供两个促销入口。两者在底层都路由到**同一个 Blackbox `system` 凭证池**（cost: $0、不扣余额），区别仅在端点版本、User-Agent 和标识请求头。

| 维度 | **fx 通路**（默认） | **eve 通路** |
| --- | --- | --- |
| 端点 | `/v3/ai/language-model` | `/v4/ai/language-model` |
| HTTP `User-Agent` | `fx/0.0.3` | `eve/0.39.1 ai-sdk-agent/tool-loop ...` |
| `body.headers` | `{user-agent, x-title}` | `{user-agent, x-title}` |
| 额外请求头 | `HTTP-Referer: github.com/vercel-labs/fx` | `ai-gateway-auth-method: api-key` |
| API key | 同一把 `vck_...` | 同一把 `vck_...` |
| 路由到的 provider | Blackbox（`credentialType: system`） | Blackbox（`credentialType: system`） |
| 计费 | $0 | $0 |

### 真正触发免费池的开关

促销**不**以 key、IP 或端点版本为键。网关识别促销请求靠**两个必须同时存在的标记**：

1. HTTP `User-Agent` 以 `fx/` 或 `eve/` 开头（大小写敏感）
2. 请求体 `headers` 对象**同时包含** `user-agent` **和** `x-title`

缺任一标记，网关返回 `customer_verification_required`（要求绑定信用卡）。UA 的具体值不重要，只要前缀匹配即可。

### 需要加 eve 通路吗？

**不需要。** 本代理默认用 fx 通路，对免费促销而言与 eve 等价：

- 同一把 key、同一个 Blackbox `system` 凭证、同样 $0、同样不扣余额
- 交叉验证：`fx key + eve UA` ✓、`eve key + fx UA` ✓ —— 全部抵达免费池
- 免费层的**限速**是按账号×按模型，换通路**不能**绕过限速

没有实现 eve 通路是因为它只增复杂度、零收益。如确需切换，可用环境变量覆盖通路标记：

```bash
FX_USER_AGENT="eve/0.39.1" UPSTREAM_URL=https://ai-gateway.vercel.sh/v4/ai/language-model uv run fx-gateway-proxy.py
```

---

## 🚦 限速机制与智能自适应重试

免费层强制**按账号、按模型**的限速。**不**是按 IP，在同一 Vercel 团队账号下换 IP 无法重置。超限返回 `429 rate_limit_exceeded`，且 `providerAttemptCount: 0`（请求在网关层就被拦截，根本没到 provider）。

### 实测表现

- 串行请求：几乎不限速
- 中等并发（约 20 并行）：少量 429，多数成功
- 高并发（约 30+ 并行）：大量 429
- 恢复窗口：短，约几十秒

### 内置智能退避与多 Key 轮换

代理自动重试瞬时失败——流式和非流式路径都覆盖。可重试状态码：`429, 500, 502, 503, 504`，以及网络/连接异常。

```python
MAX_RETRIES  = 5                # 环境变量 FX_MAX_RETRIES
BASE_DELAY   = 0.8s            # 环境变量 FX_BASE_DELAY
MAX_DELAY    = 20.0s           # 环境变量 FX_MAX_DELAY
delay(attempt) = min(BASE_DELAY * 2**attempt, MAX_DELAY)
# 退避序列：0.8 → 1.6 → 3.2 → 6.4 → 12.8（封顶 20s）
```

- **多 Key 场景**：遇到 429 时，当前 Key 自动进入指数退避冷却（30s~300s），并立即零延迟切换下一把可用 Key，大幅提升高并发与 Agent 连续工具调用成功率！
- **监控端点**：执行 `curl http://127.0.0.1:18080/v1/stats` 可实时查看各 Key 的当前负载与冷却状态。

---

## 🤝 社区与友情推荐

特别鸣谢并强烈推荐关注 **[LINUX DO 社区](https://linux.do)**：
- 💡 **极客前沿**：聚焦前沿 AI 模型探索、开发利器、逆向实战与技术干货分享。
- 🌟 **真诚氛围**：秉持“真诚、友善、团结、专业”的极客文化，是开发者交流与成长的优质聚集地。

---

## 🛡️ 开源协议

[MIT](LICENSE)
