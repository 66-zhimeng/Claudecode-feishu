# 飞书 × Claude Code 本地自动化

通过飞书 WebSocket 长连接接收消息，调用本机 Claude Code CLI 执行任务，结果通过 MCP 工具自动回复到飞书。

**无需内网穿透、无需回调 URL、无需云端 Claude API。**

---

## 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              飞书用户                                    │
│                          (发送消息 / 点击卡片)                            │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         飞书开放平台                                      │
│              (WebSocket 长连接 / 消息推送 / 事件回调)                     │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ 消息事件 + 用户信息
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         app.py (主程序)                                  │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  1. WebSocket 监听器 (lark_oapi.ws.Client)                      │   │
│  │     - 接收飞书消息事件                                           │   │
│  │     - 解析消息内容、open_id、chat_id                             │   │
│  │     - 支持文本消息、图片消息                                      │   │
│  │     - 支持卡片按钮点击事件                                        │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                  │                                       │
│                                  ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  2. 消息队列与 Worker 机制 (threading + queue)                  │   │
│  │     - 异步处理消息，避免阻塞                                      │   │
│  │     - 支持管理员白名单验证                                        │   │
│  │     - 自动下载图片资源                                           │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                  │                                       │
│                                  ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  3. Claude Code 调用器 (subprocess)                             │   │
│  │     - 使用 claude -p 执行任务                                    │   │
│  │     - 使用 --continue 保持上下文连续对话                          │   │
│  │     - 构造系统提示词，引导 Claude 使用 MCP 工具回复               │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ MCP 工具调用
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       feishu_mcp.py (MCP 工具)                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  FastMCP 框架 - stdio 模式                                       │   │
│  │                                                                  │   │
│  │  工具列表：                                                       │   │
│  │  • send_feishu_reply         - 发送文本消息                      │   │
│  │  • send_feishu_rich_text    - 发送富文本消息                     │   │
│  │  • send_feishu_card         - 发送交互式卡片消息                  │   │
│  │  • send_feishu_reply_to_message - 回复指定消息                  │   │
│  │  • get_feishu_message       - 获取消息详情                      │   │
│  │  • get_feishu_chat_history - 获取群聊历史                       │   │
│  │  • recall_feishu_message   - 撤回消息                           │   │
│  │  • test_upload_file         - 测试文件上传                      │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                  │                                       │
│                                  ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  FeishuClient (飞书 API 客户端)                                 │   │
│  │  • Token 缓存管理 (避免频繁获取)                                  │   │
│  │  • 消息发送 (文本/富文本/卡片/文件)                              │   │
│  │  • 图片/文件上传                                                │   │
│  │  • 自动重试机制                                                 │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │ HTTP API 调用
                                  ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         飞书开放平台 API                                  │
│              (im/v1/messages, im/v1/files, auth/v3/...)                │
└─────────────────────────────────────────────────────────────────────────┘
```

### 核心模块说明

#### 1. app.py - 消息接收与处理中枢（主程序）

| 组件 | 功能 |
|------|------|
| `WebSocket Client` | 通过 `lark_oapi.ws.Client` 建立飞书长连接，实时接收消息 |
| `EventDispatcherHandler` | 事件分发器，注册消息接收、卡片交互等事件处理函数 |
| `消息解析器` | 提取消息内容、open_id、chat_id、消息类型 |
| `消息队列` | Python `queue.Queue`，实现异步消息处理 |
| `Claude Worker` | 调用 `claude -p` 执行任务，支持 `--continue` 保持上下文 |

#### 2. feishu_mcp.py - MCP 工具服务

| 组件 | 功能 |
|------|------|
| `FastMCP` | 基于 stdio 模式的 MCP 服务器框架 |
| `FeishuClient` | 飞书 API 封装，支持多种消息类型 |
| `TokenCache` | Tenant Access Token 缓存（默认 2 小时） |
| `白名单验证` | 可选的用户白名单机制 |
| `自动发送` | 读取类工具自动将结果推送给用户 |

#### 3. app.py - 完整版（含 GUI 自动化）

核心功能：
- `ProcessInputSender` - Windows GUI 自动化，通过剪贴板注入文本到 Claude Code 窗口
- Claude Code 进程监测与自动启动
- 支持桌面版和 CLI 版 Claude Code

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置飞书凭证

```bash
# 复制模板
cp .env.example .env

# 编辑 .env，填入你的飞书 App ID 和 App Secret
# （在飞书开发者后台 → 凭证与基础信息 中获取）
```

### 3. 飞书后台配置

1. 打开 [飞书开发者后台](https://open.feishu.cn/app)
2. **权限管理**：开通 `im:message:send_as_bot`（发送消息），确认有 `im:message:receive_v1`（接收消息）
3. **版本管理与发布** → 创建版本 → **申请发布**（必须发布，权限才生效）

### 4. 注册 MCP 工具

让 Claude Code CLI 能调用"发飞书消息"工具：

```bash
# Windows（把路径改成你的实际项目路径）
claude mcp add feishu-bot -- python D:\ceshi_python\Claudecode-feishu\feishu_mcp.py

# 验证
claude mcp list
```

> MCP 是 stdio 模式，不需要单独启动。Claude 在需要时会自动拉起 `feishu_mcp.py`。

### 5. 启动

```bash
python app.py
```

看到 `正在连接飞书长连接网关...` 后，去飞书给机器人发一条消息测试。

---

## 安全配置

**强烈建议** 限制可触发 Claude 的用户：

1. 先不设置 `FEISHU_MY_ADMIN_OPEN_ID`，发条消息给机器人
2. 在终端日志中找到 `收到指令: ... (来自: ou_xxx)`
3. 在 `.env` 中添加：`FEISHU_MY_ADMIN_OPEN_ID=ou_xxx`

之后只有你发的消息会触发 Claude。

---

## 环境变量配置

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `FEISHU_APP_ID` | ✅ | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | ✅ | 飞书应用 App Secret |
| `FEISHU_ENCRYPT_KEY` | - | 事件加密密钥（安全模式使用） |
| `FEISHU_VERIFICATION_TOKEN` | - | 事件验证 Token |
| `FEISHU_MY_ADMIN_OPEN_ID` | - | 管理员 Open ID，填写后仅此用户可触发 |
| `FEISHU_ALLOWED_OPEN_IDS` | - | MCP 工具白名单，逗号分隔 |
| `FEISHU_AUTO_SEND_RESULT` | - | 是否自动发送结果（默认 true） |
| `FEISHU_LONG_CONTENT_THRESHOLD` | - | 长内容阈值（默认 1000 字符） |
| `AUTO_CONFIRM_MODE` | - | 自动确认模式 |

---

## 排查

| 现象 | 排查 |
|------|------|
| 终端无任何日志 | 飞书后台是否已发布？是否在机器人私聊中发送？ |
| `未配置 FEISHU_APP_ID` | `.env` 文件是否存在且填写正确？ |
| `非管理员消息已忽略` | 检查 `FEISHU_MY_ADMIN_OPEN_ID` 是否与发送者一致 |
| `未找到 claude 命令` | 安装 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) |
| Claude 执行超时 | 默认 5 分钟超时，复杂任务可能需要更长时间 |

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `app.py` | **主程序**：飞书监听 + GUI 自动化 + Claude 窗口管理 |
| `feishu_mcp.py` | MCP 工具：让 Claude 能发飞书消息 |
| `.env` | 飞书凭证配置（不提交到 Git） |
| `.env.example` | 配置模板 |
| `requirements.txt` | Python 依赖 |
| `feishu_files/` | 下载的图片资源目录 |

---

## 使用场景

### 场景 1：远程指令执行

用户在飞书发送指令 → Claude Code 执行 → 结果自动回复到飞书

### 场景 2：图片分析

用户发送图片 → 自动下载 → Claude Code 分析 → 结果推送给用户

### 场景 3：卡片交互

机器人发送带按钮的卡片 → 用户点击按钮 → 触发后续处理 → 状态反馈

---

## Cursor 中使用 MCP

在 `.cursor/mcp.json` 或 `~/.cursor/mcp.json` 中添加：

```json
{
  "mcpServers": {
    "feishu-bot": {
      "command": "python",
      "args": ["D:/ceshi_python/Claudecode-feishu/feishu_mcp.py"],
      "env": {
        "FEISHU_APP_ID": "你的_App_ID",
        "FEISHU_APP_SECRET": "你的_App_Secret"
      }
    }
  }
}
```

---

## 技术栈

- **消息接收**: `lark_oapi` (飞书 SDK)
- **MCP 服务**: `FastMCP`
- **HTTP 请求**: `httpx` (异步)
- **日志**: `loguru`
- **环境变量**: `python-dotenv`
- **GUI 自动化**: `win32gui`, `win32api`, `psutil`
