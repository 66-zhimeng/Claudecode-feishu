# -*- coding: utf-8 -*-
"""
飞书 × Claude Code 本地自动化（精简版）

一键运行：python start.py
- 通过 WebSocket 长连接接收飞书消息
- 调用 Claude Code CLI（claude -p + --continue）保持上下文连续对话
- Claude 通过 MCP 工具（send_feishu_reply）将结果发回飞书
- Windows 下打开独立终端窗口运行 Claude

依赖：pip install -r requirements.txt
配置：复制 .env.example 为 .env，填入飞书凭证
"""
import sys
import os
import json
import queue
import threading
import traceback
import subprocess
import time
import re

# Windows 控制台 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        os.system("chcp 65001 >nul 2>nul")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from loguru import logger
import lark_oapi
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

# ==================== 配置 ====================
APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")

MY_ADMIN_ID = os.environ.get("FEISHU_MY_ADMIN_OPEN_ID", "").strip()
AUTO_CONFIRM_MODE = os.environ.get("AUTO_CONFIRM_MODE", "none").strip().lower()

# ==================== 状态 ====================
_confirmation_queue = queue.Queue()
_pending_confirmations = {}

# 配置 loguru
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>",
    level="INFO",
)


def _check_config():
    if not APP_ID or APP_ID == "你的_App_ID":
        logger.error("未配置 FEISHU_APP_ID，请在 .env 中填入飞书凭证")
        sys.exit(1)
    if not APP_SECRET or APP_SECRET == "你的_App_Secret":
        logger.error("未配置 FEISHU_APP_SECRET，请在 .env 中填入飞书凭证")
        sys.exit(1)


# ==================== 飞书工具 ====================
_feishu_client = None


def _get_feishu_client():
    global _feishu_client
    if _feishu_client is None:
        _feishu_client = (
            lark_oapi.Client.builder()
            .app_id(APP_ID)
            .app_secret(APP_SECRET)
            .build()
        )
    return _feishu_client


def _send_feishu_text(chat_id: str, text: str) -> bool:
    if not chat_id or not text:
        return False
    try:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(lark_oapi.JSON.marshal({"text": text}))
            .build()
        )
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        resp = _get_feishu_client().im.v1.message.create(req)
        return bool(resp and getattr(resp, "code", -1) == 0)
    except Exception as e:
        logger.warning("飞书发反馈失败: {}", e)
        return False


def _parse_message_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        try:
            obj = json.loads(content)
            return obj.get("text", content)
        except Exception:
            return content
    if isinstance(content, dict):
        return content.get("text", "")
    return str(content)


# ==================== 消息解析 ====================
def _extract_event_fields(data):
    if hasattr(data, "event"):
        event = data.event
    elif isinstance(data, dict):
        event = data.get("event")
    else:
        return None, None, None
    if not event:
        return None, None, None

    if hasattr(event, "message"):
        message = event.message
        sender = getattr(event, "sender", None)
    elif isinstance(event, dict):
        message = event.get("message")
        sender = event.get("sender")
    else:
        return None, None, None
    if not message:
        return None, None, None

    open_id = None
    if sender:
        if hasattr(sender, "sender_id"):
            sid = sender.sender_id
            open_id = getattr(sid, "open_id", None) if sid else None
        elif isinstance(sender, dict):
            sid = sender.get("sender_id") or {}
            open_id = sid.get("open_id") if isinstance(sid, dict) else getattr(sid, "open_id", None)

    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    user_text = _parse_message_content(content).strip() if content else ""
    chat_id = message.get("chat_id") if isinstance(message, dict) else getattr(message, "chat_id", None)

    return user_text, open_id, chat_id


# ==================== 消息队列与 Worker ====================
_message_queue = queue.Queue()
_is_first_message = True


def _claude_worker():
    global _is_first_message

    print("========== Claude Worker 启动 ==========", flush=True)

    while True:
        try:
            print("\n========== 等待队列消息... ==========", flush=True)
            item = _message_queue.get()
            prompt, chat_id = item if isinstance(item, tuple) else (item, None)

            print(f"========== 取出消息，chat_id={chat_id} ==========", flush=True)

            cmd = ["claude", "-p", prompt, "--allowedTools", "mcp__feishu-bot__send_feishu_reply"]
            if not _is_first_message:
                cmd.append("--continue")
                print(">>> [Claude] 继续上下文对话...", flush=True)
            else:
                print(">>> [Claude] 开始新会话...", flush=True)

            print(f">>> [Claude] 执行命令: claude -p ...", flush=True)

            # Windows: 使用 subprocess.run 配合 shell=True
            if sys.platform == "win32":
                # 直接给 prompt 加上双引号，避免中文问题
                # 注意：需要在引号前加转义
                cmd_str = f'claude -p "{prompt}" --allowedTools mcp__feishu-bot__send_feishu_reply'
                if not _is_first_message:
                    cmd_str += " --continue"

                # 使用 shell=True 并重定向输出
                result = subprocess.run(
                    cmd_str,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                )

                full_output = result.stdout + result.stderr
                print(f"\n===== Claude 输出 =====\n{full_output[:500]}...", flush=True)

                # 发送回复到飞书
                if chat_id and full_output.strip():
                    if "send_feishu_reply" not in full_output:
                        print(f">>> 发送回复到飞书: {full_output[:100]}...", flush=True)
                        _send_feishu_text(chat_id, full_output.strip()[:500])
                    else:
                        print(">>> Claude 已通过 MCP 工具发送回复", flush=True)

                print("\n>>> [Claude] 执行完成", flush=True)
                _is_first_message = False
                _message_queue.task_done()
                continue

            _is_first_message = False
            print("\n>>> [Claude] ✅ 任务完成！等待下一条飞书消息...", flush=True)

            full_output = ''.join(output_buffer)
            if "send_feishu_reply" not in full_output and chat_id and full_output.strip():
                _send_feishu_text(chat_id, full_output.strip()[:500])

            _message_queue.task_done()

        except FileNotFoundError:
            print(">>> [Claude] ❌ 未找到 claude 命令", flush=True)
            _message_queue.task_done()
        except Exception as e:
            print(f">>> [Claude] ❌ 异常: {e}", flush=True)
            logger.error("调用 Claude 异常: {}", traceback.format_exc())
            _message_queue.task_done()


# ==================== 文件下载 ====================
def _download_resource(message_id: str, file_key: str, file_type: str) -> str:
    try:
        save_dir = os.path.join(os.getcwd(), "feishu_files")
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        suffix = "png" if file_type == "image" else "bin"
        filename = f"{file_key}.{suffix}"
        filepath = os.path.join(save_dir, filename)

        if os.path.exists(filepath):
            return filepath

        logger.info(f"正在下载资源: {file_key}")
        req = lark_oapi.api.im.v1.GetMessageResourceRequest.builder() \
            .message_id(message_id).file_key(file_key).type(file_type).build()
        resp = _get_feishu_client().im.v1.message_resource.get(req)

        if not resp.success():
            logger.error(f"下载失败: code={resp.code}")
            return ""

        with open(filepath, "wb") as f:
            f.write(resp.file.read())

        return filepath
    except Exception as e:
        logger.error(f"下载异常: {e}")
        return ""


# ==================== 消息处理 ====================
def do_process(data):
    print(f"\n========== 收到飞书消息 ==========", flush=True)

    try:
        user_text, open_id, chat_id = _extract_event_fields(data)
        if not open_id:
            print("无法解析 open_id，跳过", flush=True)
            return

        if MY_ADMIN_ID and open_id != MY_ADMIN_ID:
            print(f"非管理员消息已忽略: {open_id}", flush=True)
            return

        # 确认回复
        if _pending_confirmations and user_text:
            user_lower = user_text.strip().lower()
            for confirm_id, info in list(_pending_confirmations.items()):
                if info.get("chat_id") == chat_id:
                    if user_lower in ["yes", "y", "是", "确认", "ok"]:
                        info["answer"] = "yes"
                        info["event"].set()
                        return
                    elif user_lower in ["no", "n", "否", "拒绝"]:
                        info["answer"] = "no"
                        info["event"].set()
                        return

        # 消息类型
        msg_type = "text"
        message_id = ""
        content_dict = {}

        if hasattr(data, "event") and hasattr(data.event, "message"):
            message = data.event.message
            msg_type = getattr(message, "msg_type", "text")
            message_id = getattr(message, "message_id", "")
            try:
                content_str = getattr(message, "content", "{}")
                content_dict = json.loads(content_str) if isinstance(content_str, str) else content_str
            except:
                pass
        elif isinstance(data, dict):
            event = data.get("event", {})
            message = event.get("message", {})
            msg_type = message.get("msg_type", "text")
            message_id = message.get("message_id", "")
            try:
                content_str = message.get("content", "{}")
                content_dict = json.loads(content_str) if isinstance(content_str, str) else content_str
            except:
                pass

        if content_dict.get("image_key"):
            msg_type = "image"

        final_prompt = ""

        if msg_type == "text":
            if not user_text:
                print("空文本消息，跳过", flush=True)
                return
            final_prompt = user_text
        elif msg_type == "image":
            image_key = content_dict.get("image_key")
            if image_key and message_id:
                local_path = _download_resource(message_id, image_key, "image")
                if local_path:
                    final_prompt = f"用户发送图片：{local_path}\n请分析图片内容。"
                else:
                    if chat_id:
                        _send_feishu_text(chat_id, "⚠️ 图片下载失败")
                    return
        else:
            if chat_id:
                _send_feishu_text(chat_id, f"⚠️ 暂不支持 {msg_type} 格式")
            return

        print(f"用户消息: {final_prompt[:100]}...", flush=True)

        if chat_id:
            queue_size = _message_queue.qsize()
            icon = "🖼️" if msg_type == "image" else "✅"
            msg = f"{icon} 已收到{msg_type}" + (f"，排队 {queue_size}" if queue_size > 0 else "，处理中...")
            _send_feishu_text(chat_id, msg)

        pid = os.getpid()
        claude_prompt = (
            f"【来自飞书的远程指令】\n"
            f"用户（OpenID: {open_id}）发送内容：\n{final_prompt}\n\n"
            f"你是一个后台 Agent，用户看不到你的控制台输出。\n"
            f"不要输出闲聊文本。\n"
            f"✅ 必须使用 MCP 工具 send_feishu_reply 回复用户！\n"
            f" 请立即调用工具：mcp__feishu-bot__send_feishu_reply(message='你的回复', open_id='{open_id}')\n"

        )

        _message_queue.put((claude_prompt, chat_id))
        print(f"已投递到队列（队列长度: {_message_queue.qsize()}）", flush=True)

    except Exception as e:
        logger.error("处理消息异常: {}\n{}", e, traceback.format_exc())


# ==================== 主函数 ====================
def main():
    _check_config()

    logger.info("=" * 50)
    logger.info("飞书 × Claude Code 本地自动化")
    logger.info("=" * 50)
    if MY_ADMIN_ID:
        logger.info("安全模式：仅允许 {} 触发", MY_ADMIN_ID)
    else:
        logger.warning("未设置管理员，所有人均可触发")

    worker = threading.Thread(target=_claude_worker, daemon=True)
    worker.start()

    print("\n" + "=" * 50, flush=True)
    print("  等待飞书消息中...", flush=True)
    print("  📌 Windows：Claude 将在独立终端窗口中运行", flush=True)
    print("=" * 50 + "\n", flush=True)

    def _noop(_data):
        pass

    event_handler = (
        lark_oapi.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(do_process)
        .register_p1_customized_event("im.message.receive_v1", do_process)
        .register_p2_im_message_message_read_v1(_noop)
        .register_p2_im_message_recalled_v1(_noop)
        .build()
    )

    client = lark_oapi.ws.Client(
        APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark_oapi.LogLevel.INFO
    )
    client.start()


if __name__ == "__main__":
    main()
