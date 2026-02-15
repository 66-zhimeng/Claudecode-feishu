# 飞书 × Claude Code 本地自动化

通过飞书 WebSocket 长连接接收消息，调用本机 Claude Code CLI 执行任务，结果通过 MCP 工具自动回复到飞书。

**无需内网穿透、无需回调 URL、无需云端 Claude API。**

## 架构

```
飞书用户发消息
    ↓ 飞书服务器推事件（WebSocket）
start.py 接收消息
    ↓ 构造 prompt，调用 claude -p
Claude Code CLI 执行任务
    ↓ 调用 MCP 工具 send_feishu_reply
feishu_mcp.py → 飞书 Open API → 回复用户
```

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
python start.py
```

看到 `正在连接飞书长连接网关...` 后，去飞书给机器人发一条消息测试。

## 安全配置

**强烈建议** 限制可触发 Claude 的用户：

1. 先不设置 `FEISHU_MY_ADMIN_OPEN_ID`，发条消息给机器人
2. 在终端日志中找到 `收到指令: ... (来自: ou_xxx)`
3. 在 `.env` 中添加：`FEISHU_MY_ADMIN_OPEN_ID=ou_xxx`

之后只有你发的消息会触发 Claude。

## 排查

| 现象 | 排查 |
|------|------|
| 终端无任何日志 | 飞书后台是否已发布？是否在机器人私聊中发送？ |
| `未配置 FEISHU_APP_ID` | `.env` 文件是否存在且填写正确？ |
| `非管理员消息已忽略` | 检查 `FEISHU_MY_ADMIN_OPEN_ID` 是否与发送者一致 |
| `未找到 claude 命令` | 安装 [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) |
| Claude 执行超时 | 默认 5 分钟超时，复杂任务可能需要更长时间 |

## 文件说明

| 文件 | 作用 |
|------|------|
| `start.py` | 主程序：飞书监听 + 调用 Claude |
| `feishu_mcp.py` | MCP 工具：让 Claude 能发飞书消息 |
| `.env` | 飞书凭证配置（不提交到 Git） |
| `.env.example` | 配置模板 |
| `requirements.txt` | Python 依赖 |

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
