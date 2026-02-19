# -*- coding: utf-8 -*-
"""
飞书 × Claude Code 整合应用

功能：
1. 启动 Claude Code
2. 监测 Claude Code 进程
3. 通过剪贴板将飞书消息注入到 Claude Code 窗口

运行：python app.py
依赖：pip install -r requirements.txt
配置：复制 .env.example 为 .env，填入飞书凭证
"""
import sys
import os
import json
import queue
import threading
import time
import subprocess
from typing import Optional, List

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

# ==================== 配置 ====================
APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
ENCRYPT_KEY = os.environ.get("FEISHU_ENCRYPT_KEY", "")
VERIFICATION_TOKEN = os.environ.get("FEISHU_VERIFICATION_TOKEN", "")
MY_ADMIN_ID = os.environ.get("FEISHU_MY_ADMIN_OPEN_ID", "").strip()

# Claude Code 配置
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", r"C:\Users\yq\.local\bin\claude.exe").strip()
WORK_DIR = os.environ.get("WORK_DIR", r"D:\ceshi_python\Claudecode-feishu").strip()
PROCESS_NAME = os.environ.get("CLAUDE_PROCESS_NAME", "claude.exe").strip()

# ==================== 多工作目录管理 ====================
_workspaces: List[dict] = []  # 工作目录列表 [{"name": "xxx", "path": "xxx"}, ...]
_current_workspace_index: int = 0  # 当前工作目录索引


def load_workspace_configs() -> List[dict]:
    """从环境变量加载多工作目录配置"""
    global _workspaces

    # 优先使用 WORK_DIRS（逗号分隔的多个目录）
    work_dirs_str = os.environ.get("WORK_DIRS", "").strip()
    if work_dirs_str:
        dir_list = [d.strip() for d in work_dirs_str.split(",") if d.strip()]
        _workspaces = []
        for dir_path in dir_list:
            # 从路径提取目录名作为显示名称
            name = os.path.basename(dir_path.rstrip("\\/")) or dir_path
            _workspaces.append({"name": name, "path": dir_path})
        logger.info(f"Loaded {len(_workspaces)} workspaces")
        for ws in _workspaces:
            logger.info(f"  - {ws['name']}: {ws['path']}")
        return _workspaces

    # 兼容旧版：使用单个 WORK_DIR
    if WORK_DIR:
        _workspaces = [{"name": os.path.basename(WORK_DIR.rstrip("\\/")) or WORK_DIR, "path": WORK_DIR}]
        logger.info(f"使用单个工作目录: {_workspaces[0]['name']}")
        return _workspaces

    _workspaces = []
    return _workspaces


def get_current_workspace() -> dict:
    """获取当前工作目录"""
    if _workspaces and 0 <= _current_workspace_index < len(_workspaces):
        return _workspaces[_current_workspace_index]
    return {"name": "未知", "path": ""}


def switch_workspace(index: int) -> bool:
    """切换到指定索引的工作目录"""
    global _current_workspace_index
    if 0 <= index < len(_workspaces):
        _current_workspace_index = index
        logger.info(f"已切换到工作目录: {get_current_workspace()['name']}")
        return True
    return False


def get_workspace_display_text() -> str:
    """获取工作目录显示文本"""
    if not _workspaces:
        return "⚠️ 未配置任何工作目录"

    current = get_current_workspace()
    lines = [f"**当前目录**: {current['name']}", "", "**可选目录**:", ""]
    for i, ws in enumerate(_workspaces):
        prefix = "👉 " if i == _current_workspace_index else "   "
        lines.append(f"{prefix}{i + 1}. {ws['name']}")
    return "\n".join(lines)

# ==================== GUI 自动化 ====================
import ctypes
import win32gui
import win32con
import win32api
import win32clipboard
import win32process
import psutil

user32 = ctypes.windll.user32


class ProcessInputSender:
    """通过剪贴板将文本注入到目标进程窗口。Claude Code 无独立窗口，默认使用其所在 cmd/PowerShell 窗口。"""
    DEFAULT_PROCESS_NAMES = ("claude.exe", "claude")
    # Claude CLI 模式：终端进程
    TERMINAL_PROCESS_NAMES = ("cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe")
    # Claude 无自己的窗口，只使用这些宿主终端进程的窗口
    HOST_TERMINAL_NAMES = ("cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "windows terminal.exe")

    def __init__(self, process_name: str):
        self.process_name = (process_name or "claude.exe").strip().lower()
        self.hwnd: Optional[int] = None
        self.pid: Optional[int] = None

    def find_process_and_window(self) -> bool:
        """查找 Claude 进程，并直接使用其父进程（cmd/PowerShell）的窗口"""

        # 优先尝试查找 CLI 版本（终端中运行的 claude 命令）
        if self._find_cli_process():
            return True

        # 其次尝试查找桌面版
        return self._find_desktop_process()

    def _find_terminal_window(self, terminal_pid: int, terminal_name: str = "") -> bool:
        """查找终端进程的窗口"""
        host_pid = terminal_pid
        host_candidates: List[tuple] = []

        def host_callback(hwnd, _):
            try:
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid != host_pid:
                    return True
                visible = win32gui.IsWindowVisible(hwnd)
                known = terminal_name in ProcessInputSender.TERMINAL_PROCESS_NAMES
                host_candidates.append((hwnd, visible, known))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(host_callback, None)
        # 优先：已知终端且可见 > 已知终端 > 可见 > 任意
        host_candidates.sort(key=lambda x: (not x[2], not x[1], 0))
        if host_candidates:
            self.hwnd = host_candidates[0][0]
            self.pid = host_pid
            logger.debug("使用终端窗口 hwnd={} ({})", self.hwnd, terminal_name)
            return True

        return False

    def _find_cli_process(self) -> bool:
        """查找 CLI 版本 - 终端中运行的 claude 命令"""
        logger.debug("尝试查找 Claude CLI 进程...")

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                info = proc.info
                cmdline = info.get('cmdline') or []
                cmdline_str = ' '.join(cmdline).lower() if cmdline else ''

                # 检查命令行是否包含 claude（但不是 claude.exe 进程）
                name_lower = info.get('name', '').lower()
                if 'claude' in cmdline_str and not name_lower.startswith('claude'):
                    # 找到在终端中运行的 claude
                    pid = info['pid']
                    parent = psutil.Process(pid).parent()
                    if not parent:
                        continue

                    parent_name = parent.name().lower()
                    logger.debug("找到 CLI 进程: pid={}, 终端={}", pid, parent_name)

                    # 查找终端窗口
                    if self._find_terminal_window(parent.pid, parent_name):
                        logger.info("找到 Claude CLI 窗口 (终端: {})", parent_name)
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return False

    def _find_desktop_process(self) -> bool:
        """查找桌面版 - claude.exe 进程"""
        logger.debug("尝试查找 Claude 桌面版进程...")

        names_to_try = [self.process_name]
        if self.process_name not in ProcessInputSender.DEFAULT_PROCESS_NAMES:
            names_to_try.extend(("claude.exe", "claude"))

        target_pids: List[int] = []
        seen: set = set()
        for name_key in names_to_try:
            for proc in psutil.process_iter(['name', 'pid']):
                try:
                    pname = (proc.info.get('name') or '').lower()
                    pid = proc.info.get('pid')
                    if pid in seen:
                        continue
                    if name_key in pname or pname in name_key:
                        target_pids.append(pid)
                        seen.add(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if target_pids:
                break

        if not target_pids:
            logger.debug("未找到 Claude 相关进程 (尝试名: {})", names_to_try)
            return False

        # 遍历每个 Claude 进程，取第一个能找到「父进程窗口」的（cmd/PowerShell/或任意宿主如 Cursor）
        for claude_pid in target_pids:
            self.pid = claude_pid
            try:
                parent = psutil.Process(claude_pid).parent()
                if not parent:
                    continue
                parent_name = (parent.name() or "").lower()
                host_pid = parent.pid
                host_candidates: List[tuple] = []  # (hwnd, is_visible, is_known_terminal)

                def host_callback(hwnd, _):
                    try:
                        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                        if found_pid != host_pid:
                            return True
                        visible = win32gui.IsWindowVisible(hwnd)
                        known = parent_name in ProcessInputSender.HOST_TERMINAL_NAMES
                        host_candidates.append((hwnd, visible, known))
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(host_callback, None)
                # 优先：已知终端且可见 > 已知终端 > 可见 > 任意
                host_candidates.sort(key=lambda x: (not x[2], not x[1], 0))
                if host_candidates:
                    self.hwnd = host_candidates[0][0]
                    logger.debug("使用宿主窗口 hwnd={} ({} pid={})", self.hwnd, parent_name, host_pid)
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                continue

        logger.debug("找到 Claude 进程但父进程无可用窗口")
        return False

    def activate_window(self):
        """激活窗口（跳过激活直接尝试粘贴，Windows 限制下激活经常失败）"""
        if not self.hwnd:
            return

        # 简化处理：直接尝试激活一次，失败则跳过
        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            pass

        time.sleep(0.2)

    def send_text_via_clipboard(self, text: str):
        """通过剪贴板粘贴发送（支持中文）"""
        if not self.hwnd:
            return

        # 先激活窗口并等待
        self.activate_window()
        time.sleep(0.3)

        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        time.sleep(0.3)

        # 模拟 Ctrl+V - 增加延迟确保窗口准备好
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(ord('V'), 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.3)

    def press_enter(self):
        """发送回车键"""
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        time.sleep(0.1)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)

    def execute(self, command: str):
        self.activate_window()
        self.send_text_via_clipboard(command)
        self.press_enter()


# ==================== Claude Code 启动器 ====================
def launch_claude_code(workspace: dict = None):
    """启动 Claude Code（跳过权限确认提示）

    Args:
        workspace: 工作目录信息 {"name": "xxx", "path": "xxx"}，若不传则使用当前工作目录
    """
    # 确定使用的工作目录
    if workspace is None:
        workspace = get_current_workspace()

    work_dir = workspace.get("path", WORK_DIR)
    workspace_name = workspace.get("name", "默认")

    logger.info(f"正在启动 Claude Code (工作目录: {workspace_name}): {CLAUDE_PATH}")

    os.chdir(work_dir)

    # 添加 --dangerously-skip-permissions 跳过 "Do you want to proceed?" 确认
    cmd = [CLAUDE_PATH, "--dangerously-skip-permissions"]

    try:
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        logger.info(f"✅ Claude Code 已启动 (目录: {workspace_name})")
        return True
    except Exception as e:
        logger.error(f"❌ 启动 Claude Code 失败: {e}")
        return False


def wait_for_claude_window(sender: ProcessInputSender, timeout: int = 30) -> bool:
    """等待 Claude Code 窗口出现"""
    logger.info(f"等待 Claude Code 窗口出现 (超时 {timeout}秒)...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        if sender.find_process_and_window():
            logger.info("✅ Claude Code 窗口已就绪")
            return True
        time.sleep(1)

    logger.warning("⚠️ 等待窗口超时，请手动启动 Claude Code")
    return False


# ==================== 飞书机器人 ====================
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
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
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


def _send_workspace_selection_card(chat_id: str, open_id: str = None):
    """发送工作目录选择卡片"""
    if not _workspaces:
        _send_feishu_text(chat_id, "⚠️ 未配置任何工作目录，请检查 WORK_DIRS 环境变量")
        return

    # 构建按钮列表
    actions = []
    for i, ws in enumerate(_workspaces):
        # 每个按钮的 value 包含索引和目录名
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"📁 {ws['name']}"},
            "type": "primary" if i == _current_workspace_index else "default",
            "action_id": f"ws_select_{i}",
            "value": {"index": str(i), "name": ws['name']}
        })

    # 构建卡片内容
    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📂 选择工作目录"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "markdown",
                "content": get_workspace_display_text()
            },
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "点击下方按钮切换工作目录，切换后将自动启动对应目录的 Claude Code"}
            },
            {
                "tag": "action",
                "actions": actions
            }
        ]
    }

    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(lark_oapi.JSON.marshal(card_content))
            .build()
        )
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        resp = _get_feishu_client().im.v1.message.create(req)
        if not (resp and getattr(resp, "code", -1) == 0):
            logger.warning(f"发送工作目录卡片失败: {resp}")
    except Exception as e:
        logger.error(f"发送工作目录卡片异常: {e}")


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


def _extract_action_callback_fields(data):
    """提取卡片按钮点击事件的信息"""
    try:
        # 处理字典类型的事件数据
        if isinstance(data, dict):
            event = data.get("event", {})
            if not event:
                return None, None, None

            # 获取 sender 信息
            sender = event.get("sender", {})
            sender_id = sender.get("sender_id", {}) if sender else {}
            open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None

            # 获取 action 信息
            action = event.get("action", {})
            action_id = action.get("action_id") if action else None
            value = action.get("value") if action else None

            # 获取 chat_id
            message = event.get("message", {})
            chat_id = message.get("chat_id") if message else None

            if action_id:
                # 构建交互结果消息
                interaction_result = f"【卡片交互】用户点击了按钮: {action_id}"
                if value:
                    interaction_result += f"\n参数: {json.dumps(value, ensure_ascii=False)}"

                return interaction_result, open_id, chat_id
    except Exception as e:
        logger.error("解析卡片交互事件失败: {}", e)

    return None, None, None


# ==================== 消息处理 ====================
_message_queue = queue.Queue()
_sender: Optional[ProcessInputSender] = None


def _check_config():
    """检查配置"""
    if not APP_ID or APP_ID == "你的_App_ID":
        logger.error("未配置 FEISHU_APP_ID，请在 .env 中填入飞书凭证")
        sys.exit(1)
    if not APP_SECRET or APP_SECRET == "你的_App_Secret":
        logger.error("未配置 FEISHU_APP_SECRET，请在 .env 中填入飞书凭证")
        sys.exit(1)


def do_process(data):
    """处理飞书消息"""
    global _sender

    try:
        user_text, open_id, chat_id = _extract_event_fields(data)
        if not open_id:
            logger.info("无法解析 open_id，跳过")
            return

        if MY_ADMIN_ID and open_id != MY_ADMIN_ID:
            logger.info(f"非管理员消息已忽略: {open_id}")
            return

        # 解析消息内容
        msg_type = "text"
        if hasattr(data, "event") and hasattr(data.event, "message"):
            message = data.event.message
            msg_type = getattr(message, "msg_type", "text")
        elif isinstance(data, dict):
            event = data.get("event", {})
            message = event.get("message", {})
            msg_type = message.get("msg_type", "text")

        if msg_type != "text":
            if chat_id:
                _send_feishu_text(chat_id, f"⚠️ 暂不支持 {msg_type} 格式")
            return

        if not user_text:
            logger.info("空文本消息，跳过")
            return

        logger.info(f"收到飞书消息: {user_text[:50]}... (open_id: {open_id})")

        # 处理工作目录切换命令
        user_text_lower = user_text.strip().lower()
        if user_text_lower in ["/切换", "/目录", "/workspace", "/ws"]:
            # 发送工作目录选择卡片
            _send_workspace_selection_card(chat_id, open_id)
            return

        # 处理数字选择切换目录（从卡片点击传来的数字）
        if user_text_lower.isdigit():
            idx = int(user_text_lower) - 1
            if switch_workspace(idx):
                ws = get_current_workspace()
                _send_feishu_text(chat_id, f"✅ 已切换到工作目录: **{ws['name']}**\n路径: {ws['path']}")
                # 启动新工作目录的 Claude Code
                launch_claude_code(ws)
            return

        # 直接投递到队列 (包含 open_id 用于后续回复)，不再额外发状态提醒到飞书
        _message_queue.put((user_text, open_id, chat_id))

    except Exception as e:
        logger.error("处理消息异常: {}", e)


def do_action_callback(data):
    """处理卡片按钮点击事件"""
    try:
        # 提取交互信息
        interaction_text, open_id, chat_id = _extract_action_callback_fields(data)

        if not interaction_text:
            logger.info("无法解析卡片交互事件，跳过")
            return

        # 管理员验证
        if MY_ADMIN_ID and open_id != MY_ADMIN_ID:
            logger.info(f"非管理员卡片交互已忽略: {open_id}")
            return

        logger.info(f"收到卡片交互: {interaction_text[:50]}...")

        # 检查是否是工作目录选择按钮
        if "ws_select_" in interaction_text:
            # 解析按钮参数
            try:
                # 格式: 【卡片交互】用户点击了按钮: ws_select_X\n参数: {"index": "X", "name": "xxx"}
                import re
                match = re.search(r'"index":\s*"(\d+)"', interaction_text)
                if match:
                    idx = int(match.group(1))
                    if switch_workspace(idx):
                        ws = get_current_workspace()
                        _send_feishu_text(chat_id, f"✅ 已切换到工作目录: **{ws['name']}**\n路径: {ws['path']}")
                        # 启动新工作目录的 Claude Code
                        launch_claude_code(ws)
                        return
            except Exception as e:
                logger.error(f"解析工作目录选择失败: {e}")

        # 通知用户已收到
        if chat_id:
            _send_feishu_text(chat_id, "✅ 收到交互，正在处理...")

        # 投递到队列
        _message_queue.put((interaction_text, open_id, chat_id))

    except Exception as e:
        logger.error("处理卡片交互异常: {}", e)


def _message_worker():
    """消息处理 worker"""
    global _sender

    while True:
        try:
            item = _message_queue.get()
            # 支持新版格式: (user_text, open_id, chat_id) 和旧版格式: (user_text, chat_id)
            if isinstance(item, tuple) and len(item) >= 3:
                user_text, open_id, chat_id = item[0], item[1], item[2]
            else:
                user_text, chat_id = item if isinstance(item, tuple) else (item, None)
                open_id = None

            logger.info("正在注入消息到 Claude Code...")

            # 刷新窗口句柄
            if not _sender.find_process_and_window():
                logger.error(
                    "未找到 Claude Code 窗口。请确保 Claude Code 已启动且未关闭；"
                    "若在系统托盘，请先点击还原窗口。"
                )
                if chat_id:
                    _send_feishu_text(
                        chat_id,
                        "❌ 未找到 Claude Code 窗口，请先启动或还原 Claude Code 后再试。"
                    )
                _message_queue.task_done()
                continue

            # 构造带飞书标记的消息，提示 Claude 使用 feishu-bot MCP 回复
            is_card_interaction = user_text.startswith("【卡片交互】")

            if is_card_interaction:
                # 卡片交互消息
                feishu_marker = f"""【系统提示】此消息来自飞书（卡片交互回调）。
- 用户已点击卡片按钮，请根据用户的操作继续处理
- 请使用飞书机器人 MCP 工具将结果传回给用户

交互内容：
{user_text}"""
            else:
                # 普通文本消息
                feishu_marker = f"""【系统提示】此消息来自飞书。
- 请使用飞书机器人 MCP 工具将结果传回给用户

用户消息：
{user_text}"""

            # 执行注入
            _sender.execute(feishu_marker)
            logger.info("✅ 消息已注入")

            _message_queue.task_done()

        except Exception as e:
            logger.error("消息处理异常: {}", e)
            _message_queue.task_done()


# ==================== 主程序 ====================
def main():
    global _sender

    _check_config()

    logger.info("=" * 50)
    logger.info("飞书 × Claude Code 整合应用")
    logger.info("=" * 50)

    # 0. 加载工作目录配置
    load_workspace_configs()

    # 1. 初始化 GUI 自动化
    _sender = ProcessInputSender(PROCESS_NAME)

    # 2. 启动 Claude Code（使用当前工作目录）
    if not launch_claude_code():
        logger.error("启动 Claude Code 失败，程序退出")
        sys.exit(1)

    # 3. 等待窗口就绪
    if not wait_for_claude_window(_sender):
        logger.warning("继续运行，请确保 Claude Code 已启动")

    # 4. 启动消息处理 worker
    worker = threading.Thread(target=_message_worker, daemon=True)
    worker.start()

    # 5. 启动飞书 WebSocket
    logger.info("=" * 50)
    logger.info("等待飞书消息中...")
    logger.info("=" * 50 + "\n")

    def _noop(_data):
        pass

    event_handler = (
        lark_oapi.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(do_process)
        .register_p1_customized_event("im.message.receive_v1", do_process)
        .register_p1_customized_event("im.action.callback", do_action_callback)  # 卡片按钮点击事件
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
