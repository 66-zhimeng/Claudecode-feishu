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
import tempfile
from typing import Optional, Dict, Any

try:
    from dotenv import load_dotenv
    # 尝试从当前目录和脚本所在目录加载 .env
    import os as _os
    _script_dir = _os.path.dirname(_os.path.abspath(__file__))
    _env_paths = [
        _os.path.join(_script_dir, '.env'),
        _os.path.join(_os.getcwd(), '.env'),
    ]
    for _env_path in _env_paths:
        if _os.path.exists(_env_path):
            load_dotenv(_env_path)
            break
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

# 调试：打印环境变量加载情况
logger.info(f"FEISHU_APP_ID loaded: {bool(APP_ID)}, value: {APP_ID[:10]}..." if APP_ID else "FEISHU_APP_ID loaded: False, value: EMPTY")
logger.info(f"FEISHU_APP_SECRET loaded: {bool(APP_SECRET)}, value: {APP_SECRET[:10]}..." if APP_SECRET else "FEISHU_APP_SECRET loaded: False, value: EMPTY")

# 白名单配置（可选，填写后只允许发送给这些用户）
ALLOWED_OPEN_IDS = os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").strip()
ALLOWED_OPEN_IDS_LIST = [oid.strip() for oid in ALLOWED_OPEN_IDS.split(",") if oid.strip()] if ALLOWED_OPEN_IDS else []

# 自动发送结果开关（读取类工具是否自动发送结果给用户）
AUTO_SEND_RESULT = os.environ.get("FEISHU_AUTO_SEND_RESULT", "true").strip().lower() == "true"

# 长内容阈值（超过此字符数生成并上传markdown文件）
LONG_CONTENT_THRESHOLD = int(os.environ.get("FEISHU_LONG_CONTENT_THRESHOLD", "1000").strip())
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


def get_default_open_id() -> str:
    """从环境变量获取默认的 open_id"""
    return os.getenv("FEISHU_DEFAULT_OPEN_ID", "")


def get_default_chat_id() -> str:
    """从环境变量获取默认的 chat_id"""
    return os.getenv("FEISHU_DEFAULT_CHAT_ID", "")


def get_current_chat_id() -> str:
    """获取当前工作区的 chat_id（自动传递机制）

    优先级：
    1. 当前工作目录的配置文件 .feishu_current_chat_id（实时更新）
    2. 当前工作目录 .env 文件中的 FEISHU_CURRENT_CHAT_ID（动态更新）
    3. 环境变量 FEISHU_CURRENT_CHAT_ID
    4. 环境变量 FEISHU_DEFAULT_CHAT_ID（静态配置）
    """
    # 1. 优先从 .feishu_current_chat_id 文件读取（实时更新）
    chat_id_file = os.path.join(os.getcwd(), ".feishu_current_chat_id")
    if os.path.exists(chat_id_file):
        try:
            with open(chat_id_file, 'r', encoding='utf-8') as f:
                chat_id = f.read().strip()
                if chat_id:
                    return chat_id
        except Exception as e:
            logger.warning(f"读取 .feishu_current_chat_id 文件失败: {e}")

    # 2. 从当前工作目录的 .env 文件动态读取
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() == "FEISHU_CURRENT_CHAT_ID":
                            return v.strip()
        except Exception as e:
            logger.warning(f"读取 .env 中的 CHAT_ID 失败: {e}")

    # 3. 从环境变量读取
    env_chat_id = os.getenv("FEISHU_CURRENT_CHAT_ID", "")
    if env_chat_id:
        return env_chat_id

    # 4. 回退到静态环境变量
    return os.getenv("FEISHU_DEFAULT_CHAT_ID", "")


# 全局 Token 缓存
_token_cache = TokenCache()


# ==================== 响应构建器 ====================
def build_response(success: bool, data: Any, message: str = "") -> Dict:
    """
    构建标准化的响应结构。

    Args:
        success: 操作是否成功
        data: 响应数据
        message: 描述信息

    Returns:
        标准化响应字典
    """
    return {
        "success": success,
        "data": data,
        "message": message
    }


# ==================== 自动发送结果 ====================
async def auto_send_result(open_id: str, tool_name: str, result: Dict) -> None:
    """
    自动将结果发送给用户，根据内容类型和长度选择最佳呈现方式。

    呈现策略：
    - 短内容(<500字符)：富文本卡片
    - 中等长度(500-2000字符)：Markdown格式消息
    - 长内容(>2000字符)或结构化数据：生成文件上传

    Args:
        open_id: 用户Open ID
        tool_name: 工具名称
        result: 结果字典
    """
    if not AUTO_SEND_RESULT:
        return

    if not open_id or not validate_open_id(open_id):
        logger.debug(f"跳过自动发送: open_id={open_id}, 白名单验证={validate_open_id(open_id) if open_id else 'N/A'}")
        return

    client = get_feishu_client()
    tool_display_name = tool_name.replace("get_feishu_", "").replace("_", " ").title()

    if result.get("success"):
        data = result.get("data", {})
        message = result.get("message", "操作成功")

        # 判断内容复杂度
        content_json = json.dumps(data, ensure_ascii=False, indent=2)
        content_length = len(content_json)

        if content_length > LONG_CONTENT_THRESHOLD and isinstance(data, (dict, list)):
            # 长内容：生成Markdown文件并上传
            await _send_as_file(open_id, tool_display_name, message, data)
        else:
            # 短/中等内容：发送结构化富文本
            await _send_as_rich_content(open_id, tool_display_name, message, data)

        logger.info(f"[自动发送] {tool_name} 结果已发送给 {open_id}")
    else:
        # 失败：发送错误消息（使用正确的 post 格式）
        error_msg = result.get("message", "操作失败")
        error_content = {
            "zh_cn": {
                "title": f"❌ {tool_display_name} 失败",
                "content": [
                    [{"tag": "text", "text": "● 错误信息"}],
                    [{"tag": "text", "text": error_msg}]
                ]
            }
        }
        await client.send_message(open_id, "post", error_content)
        logger.warning(f"[自动发送] {tool_name} 失败消息已发送给 {open_id}")


async def _send_as_rich_content(target_id: str, tool_name: str, message: str, data: Any, id_type: str = "open_id") -> None:
    """发送结构化富文本内容

    Args:
        target_id: 接收者ID（open_id 或 chat_id）
        tool_name: 工具名称
        message: 消息内容
        data: 数据字典或列表
        id_type: ID类型，"open_id" 或 "chat_id"
    """
    client = get_feishu_client()

    # 构建结构化内容（使用飞书 post 消息格式）
    content_list = []

    # 标题部分
    content_list.append([{"tag": "text", "text": f"📋 {tool_name}"}])
    content_list.append([{"tag": "text", "text": f"✅ {message}"}])
    content_list.append([{"tag": "text", "text": "━━━━━━━━━━━━━━"}])

    # 数据部分 - 格式化展示
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("content", "messages"):
                # 跳过大型嵌套内容
                continue
            formatted_key = _format_key(key)
            formatted_value = _format_value(value)

            content_list.append([{"tag": "text", "text": f"● {formatted_key}"}])
            content_list.append([{"tag": "text", "text": formatted_value[:500]}])  # 限制单字段长度
    elif isinstance(data, list):
        # 列表数据
        list_items = []
        for i, item in enumerate(data[:10]):  # 最多显示10条
            if isinstance(item, dict):
                item_summary = item.get("message_id") or item.get("msg_type") or str(item)[:50]
                list_items.append(f"{i+1}. {item_summary}")
            else:
                list_items.append(f"{i+1}. {str(item)[:50]}")

        content_list.append([{"tag": "text", "text": f"● 数据列表 ({len(data)}条)"}])
        content_list.append([{"tag": "text", "text": "\n".join(list_items)}])

    rich_content = {
        "zh_cn": {
            "title": f"📋 {tool_name} 结果",
            "content": content_list
        }
    }

    await client.send_message(target_id, "post", rich_content, receive_id_type=id_type)


async def _send_as_file(open_id: str, tool_name: str, message: str, data: Any) -> None:
    """生成长内容文件并上传到飞书（真正的文件上传）"""
    client = get_feishu_client()

    # 生成Markdown内容
    md_content = _generate_markdown(tool_name, message, data)

    # 创建临时md文件
    file_name = f"{tool_name}_结果.md"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(md_content)
        temp_path = f.name

    try:
        # 1. 先发送一条提示消息（使用正确的 post 格式）
        intro_msg = {
            "zh_cn": {
                "title": f"📄 {tool_name} 结果",
                "content": [
                    [{"tag": "text", "text": f"✅ {message}"}],
                    [{"tag": "text", "text": ""}],
                    [{"tag": "text", "text": "📎 详细内容已生成文件，请查看附件"}]
                ]
            }
        }
        await client.send_message(open_id, "post", intro_msg)

        # 2. 上传文件到飞书
        file_key = await client.upload_file(temp_path, "stream")

        if file_key:
            # 3. 发送文件消息
            result = await client.send_file_message(open_id, file_key)
            if result.get("code") == 0:
                logger.info(f"[自动发送] {tool_name} 文件已上传并发送给 {open_id}")
            else:
                # 文件上传失败，回退到卡片模式
                logger.warning(f"文件上传失败，回退到卡片模式: {result}")
                await _send_as_file_fallback(open_id, tool_name, md_content)
        else:
            # 文件上传失败，回退到卡片模式
            logger.warning("文件上传失败，回退到卡片模式")
            await _send_as_file_fallback(open_id, tool_name, md_content)

    finally:
        # 清理临时文件
        try:
            os.unlink(temp_path)
        except:
            pass


async def _send_as_file_fallback(open_id: str, tool_name: str, md_content: str) -> None:
    """文件上传失败时的回退方案：发送卡片"""
    client = get_feishu_client()

    # 去掉第一行标题
    content_clean = "\n".join(md_content.split("\n")[1:]) if md_content.startswith("#") else md_content

    file_card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📄 {tool_name} 详细内容"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "markdown",
                "content": content_clean[:6000]
            }
        ]
    }

    if len(md_content) > 6000:
        await client.send_message(open_id, "interactive", file_card)
        remaining = md_content[6000:]
        while len(remaining) > 0:
            await client.send_message(open_id, "text", {"text": remaining[:5000]})
            remaining = remaining[5000:]
    else:
        await client.send_message(open_id, "interactive", file_card)


def _format_key(key: str) -> str:
    """格式化键名"""
    # 驼峰转中文
    key_mapping = {
        "message_id": "消息ID",
        "msg_type": "消息类型",
        "content": "内容",
        "create_time": "创建时间",
        "update_time": "更新时间",
        "sender_id": "发送者ID",
        "chat_id": "群聊ID",
        "total": "总数",
        "messages": "消息列表"
    }
    return key_mapping.get(key, key.replace("_", " ").title())


def _format_value(value: Any, max_len: int = 300) -> str:
    """格式化值（避免过多Markdown符号）"""
    if value is None:
        return "无"
    elif isinstance(value, bool):
        return "是" if value else "否"
    elif isinstance(value, (int, float)):
        return str(value)
    elif isinstance(value, str):
        return value[:200] if len(value) > 200 else value
    elif isinstance(value, (dict, list)):
        # 使用代码块但避免过多符号
        return f"```{json.dumps(value, ensure_ascii=False, indent=2)[:max_len]}```"
    else:
        return str(value)


def _generate_markdown(tool_name: str, message: str, data: Any) -> str:
    """生成Markdown格式的详细内容（避免过多#号）"""
    md_lines = [
        f"📋 {tool_name} 结果",
        "",
        f"✅ {message}",
        "━━━━━━━━━━━━━━",
        ""
    ]

    if isinstance(data, dict):
        for key, value in data.items():
            if key == "messages" and isinstance(value, list):
                # 消息列表特殊处理
                md_lines.append(f"● 消息列表 ({len(value)}条)")
                md_lines.append("")
                for i, msg in enumerate(value[:20]):  # 最多20条
                    msg_type = msg.get("msg_type", "unknown")
                    msg_id = msg.get("message_id", "N/A")
                    create_time = msg.get("create_time", "N/A")

                    md_lines.append(f"{i+1}. [{msg_type}] - ID: {msg_id} - 时间: {create_time}")
                md_lines.append("")
            elif isinstance(value, (dict, list)):
                md_lines.append(f"● {_format_key(key)}")
                md_lines.append(f"```{json.dumps(value, ensure_ascii=False, indent=2)[:500]}```")
                md_lines.append("")
            else:
                md_lines.append(f"● {_format_key(key)}")
                md_lines.append(f"{value}")
                md_lines.append("")
    elif isinstance(data, list):
        md_lines.append(f"● 数据列表 ({len(data)}条)")
        md_lines.append("")
        for i, item in enumerate(data[:20]):
            md_lines.append(f"{i+1}. {str(item)[:100]}")

    md_lines.append("━━━━━━━━━━━━━━")
    md_lines.append("*由 Feishu MCP 工具自动生成*")

    return "\n".join(md_lines)


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

    async def upload_file(self, file_path: str, file_type: str = "stream") -> Optional[str]:
        """
        上传文件并返回 file_key

        Args:
            file_path: 文件路径
            file_type: 文件类型 (stream, pdf, doc, excel, ppt, mp4, mp3, image)

        Returns:
            file_key 或 None
        """
        token = await self.get_token()
        if not token:
            return None

        url = f"{self.BASE_URL}/im/v1/files"
        headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f)}
                    data = {"file_type": file_type}
                    resp = await client.post(url, headers=headers, files=files, data=data)
                    result = resp.json()
                    if result.get("code") == 0:
                        file_key = result.get("data", {}).get("file_key")
                        logger.info(f"文件上传成功, file_key: {file_key}")
                        return file_key
                    logger.error("上传文件失败: {}", result)
        except Exception as e:
            logger.error("上传文件异常: {}", e)
        return None

    async def send_file_message(self, receive_id: str, file_key: str, receive_id_type: str = "open_id") -> Dict:
        """发送文件消息"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        payload = {
            "receive_id": receive_id,
            "msg_type": "file",
            "content": content,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                return resp.json()
        except Exception as e:
            logger.error("发送文件消息失败: {}", e)
            return {"code": -1, "msg": str(e)}

    async def get_message(self, message_id: str) -> Dict:
        """获取消息详情"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                return resp.json()
        except Exception as e:
            logger.error("获取消息失败: {}", e)
            return {"code": -1, "msg": str(e)}

    async def get_chat_history(self, chat_id: str, limit: int = 20) -> Dict:
        """获取群聊历史消息"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
        }
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "limit": min(limit, 50),  # 最多50条
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers, params=params)
                return resp.json()
        except Exception as e:
            logger.error("获取群聊历史失败: {}", e)
            return {"code": -1, "msg": str(e)}

    async def reply_message(self, message_id: str, msg_type: str, content: Any) -> Dict:
        """回复指定消息"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}/reply"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                result = resp.json()
                if result.get("code") == 0:
                    return result
                logger.warning("回复消息失败: {}", result)
                return result
        except Exception as e:
            logger.error("回复消息异常: {}", e)
            return {"code": -1, "msg": str(e)}

    async def recall_message(self, message_id: str) -> Dict:
        """撤回消息"""
        token = await self.get_token()
        if not token:
            return {"code": -1, "msg": "获取 token 失败"}

        url = f"{self.BASE_URL}/im/v1/messages/{message_id}"
        headers = {
            "Authorization": f"Bearer {token}",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.delete(url, headers=headers)
                return resp.json()
        except Exception as e:
            logger.error("撤回消息失败: {}", e)
            return {"code": -1, "msg": str(e)}


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


def clean_markdown(text: str) -> str:
    """清理 Markdown 符号，转换为纯文本"""
    import re
    # 移除 **加粗** -> 加粗
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    # 移除 *斜体* -> 斜体
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    # 移除 `代码` -> 代码
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 移除 ```代码块``` -> 代码块
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0)[3:-3] if len(m.group(0)) > 6 else m.group(0), text)
    # 移除 # 标题
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    # 移除 > 引用
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    # 移除 - 列表
    text = re.sub(r'^-\s*', '', text, flags=re.MULTILINE)
    # 移除数字列表
    text = re.sub(r'^\d+\.\s*', '', text, flags=re.MULTILINE)
    return text


@mcp.tool()
async def send_feishu_reply(message: str, open_id: str = "", chat_id: str = "", should_clean_markdown: bool = True) -> str:
    """
    【必须使用此工具】将任务结果、代码分析或回答发送给飞书用户。

    Args:
        message: 要发送给用户的具体文本内容。
        open_id: 接收消息的用户 Open ID（可选，不填则从环境变量 FEISHU_DEFAULT_OPEN_ID 读取）。
        chat_id: 接收消息的群聊 ID（可选，优先级高于 open_id）。
        should_clean_markdown: 是否清理 Markdown 符号（默认 true，避免 ** 加粗显示）
    """
    # 优先使用 chat_id（群聊），其次使用 open_id（个人）
    if not chat_id:
        # 自动获取当前工作区的 chat_id
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未配置 FEISHU_DEFAULT_OPEN_ID 环境变量"

    # 白名单验证（仅对个人消息）
    if chat_id:
        logger.info(f"[MCP调用] send_feishu_reply - 发送到群聊 {chat_id}, 内容长度: {len(message)}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        logger.info(f"[MCP调用] send_feishu_reply - 发送给 {open_id}, 内容长度: {len(message)}")

    # 清理 Markdown 符号
    if should_clean_markdown:
        message = clean_markdown(message)

    client = get_feishu_client()
    if chat_id:
        result = await client.send_message(chat_id, "text", {"text": message}, receive_id_type="chat_id")
        if result.get("code") == 0:
            logger.info("文本消息已发送到群聊 {}", chat_id)
            return "✅ 消息已成功发送到群聊。"
    else:
        result = await client.send_message(open_id, "text", {"text": message})
        if result.get("code") == 0:
            logger.info("文本消息已发送给 {}", open_id)
            return "✅ 消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_interaction_receipt(action_id: str, open_id: str = "", chat_id: str = "", content: str = "") -> str:
    """
    发送卡片交互的回执消息（告诉用户已收到点击）。

    Args:
        action_id: 用户点击的按钮 ID。
        open_id: 接收消息的用户 Open ID（可选，不填则从环境变量读取）。
        chat_id: 接收消息的群聊 ID（可选，优先级高于 open_id）。
        content: 额外的回执内容。
    """
    # 优先使用 chat_id（群聊），其次使用 open_id（个人）
    if not chat_id:
        # 自动获取当前工作区的 chat_id
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未配置 FEISHU_DEFAULT_OPEN_ID 环境变量"

    # 白名单验证（仅对个人消息）
    if chat_id:
        logger.info(f"[MCP调用] send_feishu_interaction_receipt - 交互回执发送到群聊 {chat_id}, action: {action_id}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        logger.info(f"[MCP调用] send_feishu_interaction_receipt - 交互回执 {open_id}, action: {action_id}")

    receipt_msg = f"✅ 已收到你的操作: {action_id}"
    if content:
        receipt_msg += f"\n{content}"

    client = get_feishu_client()
    if chat_id:
        result = await client.send_message(chat_id, "text", {"text": receipt_msg}, receive_id_type="chat_id")
    else:
        result = await client.send_message(open_id, "text", {"text": receipt_msg})

    if result.get("code") == 0:
        return "✅ 回执已发送。"

    logger.error("发送回执失败: {}", result)
    return f"❌ 发送回执失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_rich_text(title: str, content: str, open_id: str = "", chat_id: str = "") -> str:
    """
    发送富文本消息（支持换行、加粗等格式）。

    Args:
        title: 消息标题。
        content: 消息内容（支持飞书 markdown 语法，如 \\n 换行，**加粗**）。
        open_id: 接收消息的用户 Open ID（可选，不填则从环境变量读取）。
        chat_id: 接收消息的群聊 ID（可选，优先级高于 open_id）。
    """
    # 优先使用 chat_id（群聊），其次使用 open_id（个人）
    if not chat_id:
        # 自动获取当前工作区的 chat_id
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未配置 FEISHU_DEFAULT_OPEN_ID 环境变量"

    # 白名单验证（仅对个人消息）
    if chat_id:
        logger.info(f"[MCP调用] send_feishu_rich_text - 发送到群聊 {chat_id}, 标题: {title}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        logger.info(f"[MCP调用] send_feishu_rich_text - 发送给 {open_id}, 标题: {title}")

    client = get_feishu_client()

    # 构建富文本内容（正确的 post 消息格式）
    # 将 markdown 内容转换为飞书 post 格式的段落
    lines = content.split('\n')
    content_list = []

    for line in lines:
        if line.strip():
            # 处理加粗 **text**
            import re
            # 简单处理：将整个行作为文本段落
            content_list.append([{
                "tag": "text",
                "text": line
            }])

    # 如果没有内容，添加一个空段落
    if not content_list:
        content_list.append([{"tag": "text", "text": ""}])

    rich_text_content = {
        "zh_cn": {
            "title": title,
            "content": content_list
        }
    }

    if chat_id:
        result = await client.send_message(chat_id, "post", rich_text_content, receive_id_type="chat_id")
    else:
        result = await client.send_message(open_id, "post", rich_text_content)

    if result.get("code") == 0:
        logger.info("富文本消息已发送给 {}", chat_id or open_id)
        return "✅ 富文本消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    return f"❌ 发送失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_card(title: str, content: str,
                           open_id: str = "",
                           chat_id: str = "",
                           card_type: str = "template",
                           template_color: str = "blue",
                           actions: str = "") -> str:
    """
    发送交互式卡片消息。

    Args:
        title: 卡片标题。
        content: 卡片内容（支持 markdown）。
        open_id: 接收消息的用户 Open ID（可选，不填则从环境变量读取）。
        chat_id: 接收消息的群聊 ID（可选，优先级高于 open_id）。
        card_type: 卡片类型 ("template" 模板卡片 或 "interactive" 交互卡片)。
        template_color: 模板颜色 ("blue", "green", "red", "yellow", "grey")。
        actions: 按钮配置，JSON 格式字符串，如 '[{"tag":"button","text":{"tag":"plain_text","content":"确定"},"type":"primary","action_id":"confirm"}]'
    """
    # 优先使用 chat_id（群聊），其次使用 open_id（个人）
    if not chat_id:
        # 自动获取当前工作区的 chat_id
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未配置 FEISHU_DEFAULT_OPEN_ID 环境变量"

    # 白名单验证（仅对个人消息）
    if chat_id:
        logger.info(f"[MCP调用] send_feishu_card - 发送到群聊 {chat_id}, 标题: {title}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        logger.info(f"[MCP调用] send_feishu_card - 发送给 {open_id}, 标题: {title}")

    # 调试：打印环境变量
    logger.info(f"APP_ID: {APP_ID[:10]}..." if APP_ID else "APP_ID: empty")
    logger.info(f"FEISHU_DEFAULT_CHAT_ID: {get_default_chat_id()}")

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

    # 发送到群聊或个人
    if chat_id:
        logger.info(f"正在发送卡片到群聊: chat_id={chat_id}, card_type={card_type}")
        result = await client.send_message(chat_id, "interactive", card_content, receive_id_type="chat_id")
        if result.get("code") == 0:
            logger.info("卡片消息已发送到群聊 {}", chat_id)
            return "✅ 卡片消息已成功发送到群聊。"
    else:
        logger.info(f"正在发送卡片到个人: open_id={open_id}, card_type={card_type}")
        result = await client.send_message(open_id, "interactive", card_content)
        if result.get("code") == 0:
            logger.info("卡片消息已发送给 {}", open_id)
            return "✅ 卡片消息已成功发送给用户。"

    logger.error("发送失败: {}", result)
    # 返回更详细的错误信息
    error_detail = json.dumps(result, ensure_ascii=False, indent=2)
    return f"❌ 发送失败: {result.get('msg', result)}\n\n详细信息: {error_detail}"


@mcp.tool()
async def get_feishu_message(message_id: str, open_id: str = "") -> str:
    """
    获取指定消息的详细内容。

    Args:
        message_id: 消息ID（从飞书消息事件中获取）。
        open_id: 可选，填写后会自动将结果发送给用户。

    Returns:
        结构化JSON字符串，包含success、data、message字段。
    """
    logger.info(f"[MCP调用] get_feishu_message - message_id: {message_id}, open_id: {open_id}")

    client = get_feishu_client()
    result = await client.get_message(message_id)

    if result.get("code") == 0:
        data = result.get("data", {})
        msg_type = data.get("msg_type", "unknown")
        content = data.get("content", "")

        # 解析消息内容
        content_obj = content
        try:
            if isinstance(content, str):
                content_obj = json.loads(content)
        except:
            pass

        # 构建结构化响应
        response_data = {
            "message_id": data.get("message_id"),
            "msg_type": msg_type,
            "content": content_obj,
            "create_time": data.get("create_time"),
            "update_time": data.get("update_time")
        }

        response = build_response(True, response_data, "获取消息成功")

        # 自动发送结果
        if open_id:
            await auto_send_result(open_id, "get_feishu_message", response)

        return json.dumps(response, ensure_ascii=False, indent=2)

    logger.error("获取消息失败: {}", result)
    response = build_response(False, {}, result.get("msg", "获取消息失败"))

    # 自动发送失败通知
    if open_id:
        await auto_send_result(open_id, "get_feishu_message", response)

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_feishu_chat_history(chat_id: str, limit: int = 20, open_id: str = "") -> str:
    """
    获取群聊的历史消息。

    Args:
        chat_id: 群聊ID（chat_id）。
        limit: 返回消息数量，默认20条，最多50条。
        open_id: 可选，填写后会自动将结果发送给用户。

    Returns:
        结构化JSON字符串，包含success、data、message字段。
    """
    logger.info(f"[MCP调用] get_feishu_chat_history - chat_id: {chat_id}, limit: {limit}, open_id: {open_id}")

    client = get_feishu_client()
    result = await client.get_chat_history(chat_id, limit)

    if result.get("code") == 0:
        items = result.get("data", {}).get("items", [])

        # 构建消息列表
        messages = []
        for msg in items:
            msg_type = msg.get("msg_type", "unknown")
            create_time = msg.get("create_time", "")
            sender_id = msg.get("sender_id", {})
            content = msg.get("content", "")

            # 解析内容
            content_obj = content
            try:
                if isinstance(content, str):
                    content_obj = json.loads(content)
            except:
                pass

            messages.append({
                "message_id": msg.get("message_id"),
                "msg_type": msg_type,
                "create_time": create_time,
                "sender_id": sender_id,
                "content": content_obj
            })

        response_data = {
            "chat_id": chat_id,
            "total": len(messages),
            "messages": messages
        }

        response = build_response(True, response_data, f"获取到 {len(messages)} 条消息")

        # 自动发送结果
        if open_id:
            await auto_send_result(open_id, "get_feishu_chat_history", response)

        return json.dumps(response, ensure_ascii=False, indent=2)

    logger.error("获取群聊历史失败: {}", result)
    response = build_response(False, {}, result.get("msg", "获取群聊历史失败"))

    # 自动发送失败通知
    if open_id:
        await auto_send_result(open_id, "get_feishu_chat_history", response)

    return json.dumps(response, ensure_ascii=False, indent=2)


@mcp.tool()
async def send_feishu_reply_to_message(message_id: str, message: str, msg_type: str = "text") -> str:
    """
    回复指定的消息（基于消息ID进行回复）。

    Args:
        message_id: 要回复的消息ID。
        message: 回复的内容。
        msg_type: 消息类型，可选 "text", "post", "interactive"，默认 "text"。
    """
    logger.info(f"[MCP调用] send_feishu_reply_to_message - 回复消息: {message_id}")

    client = get_feishu_client()

    # 根据消息类型构建内容
    if msg_type == "text":
        content = {"text": message}
    elif msg_type == "post":
        content = {
            "title": "消息",
            "sections": [{"header": "消息", "text": {"tag": "markdown", "content": message}}]
        }
    else:
        content = {"text": message}

    result = await client.reply_message(message_id, msg_type, content)

    if result.get("code") == 0:
        logger.info("回复消息成功: {}", message_id)
        return f"✅ 已回复消息（ID: {message_id}）"

    logger.error("回复消息失败: {}", result)
    return f"❌ 回复消息失败: {result.get('msg', result)}"


@mcp.tool()
async def recall_feishu_message(message_id: str) -> str:
    """
    撤回机器人发送的消息。

    Args:
        message_id: 要撤回的消息ID。

    Note:
        只能在消息发送后短时间内撤回，且只能撤回机器人自己发送的消息。
    """
    logger.info(f"[MCP调用] recall_feishu_message - 撤回消息: {message_id}")

    client = get_feishu_client()
    result = await client.recall_message(message_id)

    if result.get("code") == 0:
        logger.info("撤回消息成功: {}", message_id)
        return f"✅ 已撤回消息（ID: {message_id}）"

    logger.error("撤回消息失败: {}", result)
    return f"❌ 撤回消息失败: {result.get('msg', result)}"


@mcp.tool()
async def send_feishu_file(file_path: str, open_id: str = "", chat_id: str = "", file_type: str = "stream") -> str:
    """
    发送本地文件给飞书用户或群聊。

    支持发送任意本地文件（文档、代码、日志、图片等）。

    Args:
        file_path: 本地文件的绝对路径或相对路径。
        open_id: 接收文件的用户 Open ID（可选，不填则自动获取）。
        chat_id: 接收文件的群聊 ID（可选，优先级高于 open_id）。
        file_type: 文件类型，可选 stream/pdf/doc/excel/ppt/mp4/mp3/image，默认 stream。
    """
    # 解析目标 ID（与其他工具一致的逻辑）
    if not chat_id:
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未指定接收者，请提供 chat_id 或 open_id，或配置环境变量"

    # 确定发送目标
    if chat_id:
        target_id = chat_id
        id_type = "chat_id"
        logger.info(f"[MCP调用] send_feishu_file - 发送文件到群聊 {chat_id}, 文件: {file_path}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        target_id = open_id
        id_type = "open_id"
        logger.info(f"[MCP调用] send_feishu_file - 发送文件给 {open_id}, 文件: {file_path}")

    # 验证文件存在
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return f"❌ 文件不存在: {abs_path}"
    if not os.path.isfile(abs_path):
        return f"❌ 路径不是文件: {abs_path}"

    client = get_feishu_client()

    # 上传文件
    file_key = await client.upload_file(abs_path, file_type)
    if not file_key:
        return "❌ 文件上传失败，请检查文件大小和格式"

    # 发送文件消息
    result = await client.send_file_message(target_id, file_key, receive_id_type=id_type)
    if result.get("code") == 0:
        file_name = os.path.basename(abs_path)
        logger.info(f"文件 {file_name} 已发送给 {target_id}")
        return f"✅ 文件 {file_name} 已成功发送！"

    logger.error("发送文件失败: {}", result)
    return f"❌ 发送文件失败: {result.get('msg', '未知错误')}"


@mcp.tool()
async def test_upload_file(open_id: str = "", chat_id: str = "") -> str:
    """
    测试文件上传功能（发送测试文件给用户）。

    Args:
        open_id: 接收文件的用户 Open ID（可选，不填则自动获取）。
        chat_id: 接收文件的群聊 ID（可选，优先级高于 open_id）。

    Returns:
        上传结果。
    """
    # 解析目标 ID（与其他工具一致的逻辑）
    if not chat_id:
        chat_id = get_current_chat_id()
        if not open_id:
            open_id = get_default_open_id()
        if not open_id and not chat_id:
            return "❌ 错误：未指定接收者，请提供 chat_id 或 open_id，或配置环境变量"

    # 确定发送目标
    if chat_id:
        target_id = chat_id
        id_type = "chat_id"
        logger.info(f"[MCP调用] test_upload_file - 发送测试文件到群聊 {chat_id}")
    else:
        if not validate_open_id(open_id):
            logger.warning(f"拒绝发送给未授权用户: {open_id}")
            return "❌ 拒绝发送：用户不在白名单中"
        target_id = open_id
        id_type = "open_id"
        logger.info(f"[MCP调用] test_upload_file - 发送测试文件给 {open_id}")

    client = get_feishu_client()

    # 创建测试文件
    test_content = """# 测试文件上传功能

## 基本信息
- 操作：测试文件上传
- 状态：✅ 成功

## 数据详情

### 测试数据1
这是一段测试内容，用于验证文件上传功能是否正常工作。

### 测试数据2
```json
{"message": "hello", "data": [1, 2, 3]}
```

### 测试数据3
- 消息ID: om_test001
- 消息类型: text
- 发送者: ou_test001
- 创建时间: 1700000000

---

*由 Feishu MCP 工具自动生成*"""

    # 创建临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
        f.write(test_content)
        temp_path = f.name

    try:
        # 先发送提示消息
        await client.send_message(target_id, "text", {"text": "📤 正在上传文件..."}, receive_id_type=id_type)

        # 上传文件
        file_key = await client.upload_file(temp_path, "stream")

        if file_key:
            # 发送文件
            result = await client.send_file_message(target_id, file_key, receive_id_type=id_type)
            if result.get("code") == 0:
                return "✅ 测试文件已发送！请查看附件。"
            else:
                return f"❌ 发送失败: {result.get('msg', '未知错误')}"
        else:
            return "❌ 文件上传失败"

    finally:
        # 清理
        try:
            os.unlink(temp_path)
        except:
            pass


# ==================== 启动 ====================
if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run())
