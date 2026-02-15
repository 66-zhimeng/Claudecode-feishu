# -*- coding: utf-8 -*-
"""
MCP 工具：让 Claude Code 拥有「发飞书消息」的能力。
注册到 Claude 后，Claude 执行任务完成时可调用 send_feishu_reply 把结果发回用户。

注册方式：claude mcp add feishu-bot -- python feishu_mcp.py
"""
import json
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
from fastmcp import FastMCP
from loguru import logger

# 配置 loguru
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>",
    level="INFO",
)

# ==================== 配置 ====================
APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
# ==============================================

if not APP_ID or not APP_SECRET:
    logger.warning("未配置 FEISHU_APP_ID 或 FEISHU_APP_SECRET，发送飞书消息将失败")

mcp = FastMCP("Feishu-Bot")


async def get_tenant_access_token():
    """获取飞书 tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
        data = resp.json()
        return data.get("tenant_access_token")


@mcp.tool()
async def send_feishu_reply(message: str, open_id: str) -> str:
    """
    【必须使用此工具】将任务结果、代码分析或回答发送给飞书用户。

    Args:
        message: 要发送给用户的具体文本内容。
        open_id: 接收消息的用户 Open ID（由「来自飞书的远程指令」中的用户OpenID 提供）。
    """
    token = await get_tenant_access_token()
    if not token:
        return "❌ 获取飞书 token 失败，请检查 APP_ID / APP_SECRET。"

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": message}, ensure_ascii=False),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload)
        result = resp.json()

    if result.get("code") == 0:
        logger.info("消息已发送给 {}", open_id)
        return "✅ 消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result}"


if __name__ == "__main__":
    mcp.run()
