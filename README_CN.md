# FX Gateway Proxy

<div align="center">

[![LINUX DO](https://img.shields.io/badge/社区-LINUX%20DO-blue?style=flat&logo=discourse&logoColor=white)](https://linux.do)
[![CI](https://github.com/Xeron2000/fx-gateway-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/Xeron2000/fx-gateway-proxy/actions)
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
| `zai/glm-5.2` | 智谱 AI / ZAI | 1,000,000 | 128,000 | 深度思考 (Reasoning)、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |
| `zai/glm-5.2-fast` | 智谱 AI / ZAI | 1,000,000 | 128,000 | 极速响应、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |

---

## ✨ 核心特性

- 🔄 **自适应多 Key 智能路由**：
  - 支持配置多把 Key，基于 60s 滑动窗口自动追踪各 Key 的 Request/Token 负载；
  - 动态学习每个 Key 的有效 RPM/TPM 上限，加权优先调度低负载 Key；
  - 遇到 429 速率限制时自动加入指数冷却退避（基准 30s，封顶 300s），并无感秒级轮换下一把可用 Key。
- 📊 **实时指标监控**：提供 `GET /v1/stats` 端点，脱敏展示各 Key 实时负载率、成功数、429 触发数及估算上限。
- 🛡️ **极限边界健壮性**：
  - 自动清洗客户端发送的空消息与空白字符，彻底避免 Vercel 400 校验错误；
  - 严格兼容 OpenAI 非流式响应规范，支持多轮工具调用闭环与 Prompt Caching 数据上报。

---

## 🔑 第一步：配置 API Key

支持以下**任意一种**配置方式（支持多 Key 轮换）：

### 方式 A：通过 `fx` CLI 登录（单 Key / 自动生成）
```bash
fx login
```
登录后密钥保存在 `~/.fx/api-key`。

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

### 方法 1：通过 `uv` / `uvx` 远端直接运行（免安装，推荐）

```bash
# 方式 A：通过 uvx 从 GitHub 仓库一键启动
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# 方式 B：通过 uv run 运行单文件脚本
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

### 方法 2：Systemd 用户服务（后台常驻守护）

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

### 方法 3：Docker 容器部署

```bash
docker compose up -d
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

**启动 Pi 体验**：
```bash
pi --provider vercel-fx --model zai/glm-5.2
```

---

### 2. 查看多 Key 运行状态

```bash
curl http://127.0.0.1:18080/v1/stats
```
输出示例：
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

## 🤝 社区与友情推荐

特别鸣谢并强烈推荐关注 **[LINUX DO 社区](https://linux.do)**：
- 💡 **极客前沿**：聚焦前沿 AI 模型探索、开发利器、逆向实战与技术干货分享。
- 🌟 **真诚氛围**：秉持“真诚、友善、团结、专业”的极客文化，是开发者交流与成长的优质聚集地。

---

## 🛡️ 开源协议

[MIT](LICENSE)
