# -*- coding: utf-8 -*-
"""
MCP 工具：让 Claude Code 拥有「发飞书消息」的能力。

功能：
- 发送文本消息
- 发送富文本消息
- 发送图片消息
- 发送交互式卡片消息

注册方式：claude mcp add feishu-bot -- python feishu_mcp.py
"""
import asyncio
import json
import os
import sys
import time
from typing import Optional, Dict, Any

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

# 白名单配置（可选，填写后只允许发送给这些用户）
ALLOWED_OPEN_IDS = os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").strip()
ALLOWED_OPEN_IDS_LIST = [oid.strip() for oid in ALLOWED_OPEN_IDS.split(",") if oid.strip()] if ALLOWED_OPEN_IDS else []
# ==============================================

if not APP_ID or not APP_SECRET:
    logger.warning("未配置 FEISHU_APP_ID 或 FEISHU_APP_SECRET，发送飞书消息将失败")

mcp = FastMCP("Feishu-Bot")


# ==================== Token 缓存 ====================
class TokenCache:
    """飞书 Token 缓存"""

    def __init__(self):
        self._token: Optional[str] = None
        self._expire_time: float = 0

    def is_valid(self) -> bool:
        """检查缓存的 token 是否有效"""
        return bool(self._token and time.time() < self._expire_time)

    def get(self) -> Optional[str]:
        """获取缓存的 token"""
        if self.is_valid():
            return self._token
        return None

    def set(self, token: str, expire_seconds: int = 7200):
        """设置 token 缓存（提前 5 分钟刷新）"""
        self._token = token
        self._expire_time = time.time() + expire_seconds - 300


# ==================== 白名单验证 ====================
def validate_open_id(open_id: str) -> bool:
    """验证 open_id 是否在白名单中"""
    if not ALLOWED_OPEN_IDS_LIST:
        # 未配置白名单，放行所有
        return True
    return open_id in ALLOWED_OPEN_IDS_LIST


# 全局 Token 缓存
_token_cache = TokenCache()


# ==================== 飞书客户端 ====================
class FeishuClient:
    """飞书 API 客户端"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret

    async def get_token(self) -> Optional[str]:
        """获取 tenant_access_token（带缓存）"""
        cached = _token_cache.get()
        if cached:
            logger.debug("使用缓存的 token")
            return cached

        url = f"{self.BASE_URL}/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={
                "app_id": self.app_id,
                "app_secret": self.app_secret
            })
            data = resp.json()
            if data.get("code") == 0:
                token = data.get("tenant_access_token")
                expire = data.get("expire", 7200)
                _token_cache.set(token, expire)
                logger.info("获取新 token 成功")
                return token
            logger.error("获取 token 失败: {}", data)
            return None

    async def send_message(self, receive_id: str, msg_type: str, content: Any,
                          receive_id_type: str = "open_id") -> Dict:
        """发送消息（带重试）"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content,
        }

        # 带重试的请求
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    result = resp.json()
                    if result.get("code") == 0:
                        return result
                    # 如果是 token 过期，尝试重新获取
                    if result.get("code") in [99991663, 99991664]:  # token 相关错误码
                        _token_cache._token = None  # 清除缓存
                        token = await self.get_token()
                        if token:
                            headers["Authorization"] = f"Bearer {token}"
                            continue
                    logger.warning("发送失败 (尝试 {}): {}", attempt + 1, result)
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                logger.warning("发送异常 (尝试 {}): {}", attempt + 1, e)
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))

        return {"code": -1, "msg": "发送失败，已重试 3 次"}

    async def upload_image(self, image_path: str) -> Optional[str]:
        """上传图片并返回 image_key"""
        token = await self.get_token()
        if not token:
            return None

        url = f"{self.BASE_URL}/im/v1/images"
        headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                with open(image_path, "rb") as f:
                    files = {"image": f}
                    data = {"image_type": "message"}
                    resp = await client.post(url, headers=headers, files=files, data=data)
                    result = resp.json()
                    if result.get("code") == 0:
                        return result.get("data", {}).get("image_key")
        except Exception as e:
            logger.error("上传图片失败: {}", e)
        return None


# 全局客户端实例
_feishu_client = None


def get_feishu_client() -> FeishuClient:
    global _feishu_client
    if _feishu_client is None:
        _feishu_client = FeishuClient(APP_ID, APP_SECRET)
    return _feishu_client


# ==================== MCP 工具 ====================

@mcp.tool()
async def get_my_open_id() -> str:
    """
    获取当前机器人应用所属人员的 open_id。

    注意：由于权限限制，可能无法获取。
    建议通过以下方式获取 open_id：
    1. 运行 app.py，查看用户发送消息时的日志
    2. 在飞书开放平台应用管理中查看
    """
    client = get_feishu_client()
    token = await client.get_token()
    if not token:
        return "❌ 获取 token 失败"

    # 尝试调用获取用户 ID API
    url = f"{client.BASE_URL}/identity/v1/end_user/get_id"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, headers=headers)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("code") == 0:
                    data = result.get("data", {})
                    open_id = data.get("open_id", "未知")
                    union_id = data.get("union_id", "未知")
                    return f"✅ open_id: {open_id}\nunion_id: {union_id}"
    except Exception as e:
        pass

    # API 失败，返回获取方法
    return """❌ 无法通过 API 获取 open_id

建议获取方式：
1. 运行 python app.py，用户发送消息后控制台会显示 open_id
2. 登录飞书开放平台 https://open.feishu.cn 查看应用信息"""


@mcp.tool()
async def send_feishu_reply(message: str, open_id: str) -> str:
    """
    【必须使用此工具】将任务结果、代码分析或回答发送给飞书用户。

    Args:
        message: 要发送给用户的具体文本内容。
        open_id: 接收消息的用户 Open ID（由「来自飞书的远程指令」中的用户OpenID 提供）。
    """
    # 白名单验证
    if not validate_open_id(open_id):
        logger.warning(f"拒绝发送给未授权用户: {open_id}")
        return "❌ 拒绝发送：用户不在白名单中"

    logger.info(f"[MCP调用] send_feishu_reply - 发送给 {open_id}, 内容长度: {len(message)}")

    client = get_feishu_client()
    result = await client.send_message(open_id, "text", {"text": message})

    if result.get("code") == 0:
        logger.info("文本消息已发送给 {}", open_id)
        return "✅ 消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_interaction_receipt(open_id: str, action_id: str, content: str = "") -> str:
    """
    发送卡片交互的回执消息（告诉用户已收到点击）。

    Args:
        open_id: 接收消息的用户 Open ID。
        action_id: 用户点击的按钮 ID。
        content: 额外的回执内容。
    """
    # 白名单验证
    if not validate_open_id(open_id):
        logger.warning(f"拒绝发送给未授权用户: {open_id}")
        return "❌ 拒绝发送：用户不在白名单中"

    receipt_msg = f"✅ 已收到你的操作: {action_id}"
    if content:
        receipt_msg += f"\n{content}"

    logger.info(f"[MCP调用] send_feishu_interaction_receipt - 交互回执 {open_id}, action: {action_id}")

    client = get_feishu_client()
    result = await client.send_message(open_id, "text", {"text": receipt_msg})

    if result.get("code") == 0:
        return "✅ 回执已发送。"

    logger.error("发送回执失败: {}", result)
    return f"❌ 发送回执失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_rich_text(open_id: str, title: str, content: str) -> str:
    """
    发送富文本消息（支持换行、加粗等格式）。

    Args:
        open_id: 接收消息的用户 Open ID。
        title: 消息标题。
        content: 消息内容（支持飞书 markdown 语法，如 \\n 换行，**加粗**）。
    """
    # 白名单验证
    if not validate_open_id(open_id):
        logger.warning(f"拒绝发送给未授权用户: {open_id}")
        return "❌ 拒绝发送：用户不在白名单中"

    logger.info(f"[MCP调用] send_feishu_rich_text - 发送给 {open_id}, 标题: {title}")

    client = get_feishu_client()

    # 构建富文本内容
    rich_text_content = {
        "title": title,
        "sections": [
            {
                "header": title,
                "text": {
                    "tag": "markdown",
                    "content": content
                }
            }
        ]
    }

    result = await client.send_message(open_id, "post", rich_text_content)

    if result.get("code") == 0:
        logger.info("富文本消息已发送给 {}", open_id)
        return "✅ 富文本消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_card(open_id: str, title: str, content: str,
                           card_type: str = "template",
                           template_color: str = "blue",
                           actions: str = "") -> str:
    """
    发送交互式卡片消息。

    Args:
        open_id: 接收消息的用户 Open ID。
        title: 卡片标题。
        content: 卡片内容（支持 markdown）。
        card_type: 卡片类型 ("template" 模板卡片 或 "interactive" 交互卡片)。
        template_color: 模板颜色 ("blue", "green", "red", "yellow", "grey")。
        actions: 按钮配置，JSON 格式字符串，如 '[{"tag":"button","text":{"tag":"plain_text","content":"确定"},"type":"primary","action_id":"confirm"}]'
    """
    # 白名单验证
    if not validate_open_id(open_id):
        logger.warning(f"拒绝发送给未授权用户: {open_id}")
        return "❌ 拒绝发送：用户不在白名单中"

    logger.info(f"[MCP调用] send_feishu_card - 发送给 {open_id}, 标题: {title}")

    client = get_feishu_client()

    # 构建卡片内容
    card_content = {
        "config": {
            "wide_screen_mode": True
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title
            },
            "template": template_color
        },
        "elements": [
            {
                "tag": "markdown",
                "content": content
            }
        ]
    }

    # 添加按钮（如果有）
    if actions:
        try:
            actions_list = json.loads(actions)
            card_content["elements"].append({
                "tag": "action",
                "actions": actions_list
            })
        except json.JSONDecodeError:
            logger.warning("actions JSON 解析失败，跳过按钮")

    result = await client.send_message(open_id, "interactive", card_content)

    if result.get("code") == 0:
        logger.info("卡片消息已发送给 {}", open_id)
        return "✅ 卡片消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result.get('msg', result)}"


# ==================== 启动 ====================
if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run())
