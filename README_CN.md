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

专为 Vercel AI Gateway 免费促销池（**GLM 5.2 / GLM 5.2 Fast**）打造的 OpenAI 兼容协议反向代理服务器。

开箱即用，原生支持 **Pi**、**Cursor**、**Cline**、**Aider**、**Claude Code** 及任意标准 OpenAI SDK。

---

## 🎯 模型支持

| 模型 ID | 提供方 | 上下文窗口 | 单次最大输出 | 特性 |
| :--- | :--- | :---: | :---: | :--- |
| `zai/glm-5.2` | 智谱 AI / ZAI | 1,000,000 | 128,000 | 深度思考 (Reasoning)、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |
| `zai/glm-5.2-fast` | 智谱 AI / ZAI | 1,000,000 | 128,000 | 极速响应、工具调用 (Tool Call)、多模态 (Vision)、前缀缓存 |

---

## 🔑 第一步：获取 Vercel AI Gateway API Key

支持以下**任意一种**方式：

### 方式 A：通过 `fx` CLI 登录（推荐，全自动）
如果本地安装了 `fx`，直接在终端执行：
```bash
fx login
```
登录成功后密钥将自动保存在 `~/.fx/api-key`。**反代服务启动时会自动识别并读取该文件。**

### 方式 B：通过 Vercel 控制台手动创建
1. 打开 [Vercel AI Gateway 控制台](https://vercel.com/~/ai-gateway/api-keys)。
2. 创建一个 API Key（格式为 `vck_...`）。
3. 设置为环境变量：
   ```bash
   export AI_GATEWAY_API_KEY="vck_你的密钥"
   ```

---

## 🚀 第二步：启动反向代理服务

服务默认监听端口：`http://127.0.0.1:18080/v1`

### 方法 1：通过 `uv` / `uvx` 远端直接运行（免安装环境，推荐）

```bash
# 方式 A：通过 uvx 从 GitHub 仓库一键启动
uvx --from git+https://github.com/Xeron2000/fx-gateway-proxy.git fx-gateway-proxy

# 方式 B：通过 uv run 运行远端单文件脚本
uv run --script https://raw.githubusercontent.com/Xeron2000/fx-gateway-proxy/main/fx-gateway-proxy.py
```

### 方法 2：Systemd 用户服务（后台常驻守护）

```bash
# 1. 复制单文件脚本
cp fx_gateway_proxy/cli.py ~/.local/bin/fx-gateway-proxy.py
chmod +x ~/.local/bin/fx-gateway-proxy.py

# 2. 配置 Systemd 服务
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

# 3. 启用并启动服务
systemctl --user daemon-reload
systemctl --user enable --now fx-gateway-proxy.service
```

### 方法 3：Docker Compose 容器部署

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

> **关于 `apiKey` 的说明**：
> - 填 `"apiKey": "dummy"`：反代会自动从 `~/.fx/api-key` 或 `AI_GATEWAY_API_KEY` 环境变量中提取真实密钥。
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
2. 环境变量：`AI_GATEWAY_API_KEY`
3. 本地凭证文件：`~/.fx/api-key`（由 `fx login` 自动生成）

---

## 🤝 社区与友情推荐

特别鸣谢并强烈推荐关注 **[LINUX DO 社区](https://linux.do)** ()：
- 💡 **极客前沿**：聚焦前沿 AI 模型探索、开发利器、逆向实战与技术干货分享。
- 🌟 **真诚氛围**：秉持“真诚、友善、团结、专业”的极客文化，是开发者交流与成长的优质聚集地。

---

## 🛡️ 开源协议

[MIT](LICENSE)
