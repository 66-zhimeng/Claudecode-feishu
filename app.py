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
from __future__ import annotations

import sys
import os
import json
import queue
import threading
import time
import subprocess
from typing import Optional, List, Dict

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

# 工作区持久化配置
WORKSPACE_PERSIST_FILE = os.environ.get("WORKSPACE_PERSIST_FILE", "workspace_persist.json").strip()

# ==================== 多工作区持久化 ====================
def _get_persist_file_path() -> str:
    """获取持久化文件路径"""
    # 使用 app.py 所在目录
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, WORKSPACE_PERSIST_FILE)


def _load_workspace_persist():
    """加载工作区会话持久化"""
    persist_file = _get_persist_file_path()
    if not os.path.exists(persist_file):
        logger.info("未找到工作区持久化文件，将创建新文件")
        return {}

    try:
        with open(persist_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"已加载工作区持久化数据: {len(data.get('workspace_chat_map', {}))} 个群聊映射")
            return data
    except Exception as e:
        logger.warning(f"加载工作区持久化失败: {e}")
        return {}


def _save_workspace_persist():
    """保存工作区会话持久化"""
    persist_file = _get_persist_file_path()
    try:
        data = _workspace_manager.get_persist_data()
        with open(persist_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug("已保存工作区持久化数据")
    except Exception as e:
        logger.warning(f"保存工作区持久化失败: {e}")


# ==================== 多工作目录管理 ====================
_workspaces: List[dict] = []  # 工作目录列表 [{"name": "xxx", "path": "xxx"}, ...]
_current_workspace_index: int = 0  # 当前工作目录索引
_admin_open_id_detected: bool = False  # 是否已检测到 admin open_id


def update_workspace_env_chat_id(workspace_dir: str, chat_id: str):
    """更新工作区 .env 文件中的 CHAT_ID"""
    if not workspace_dir or not chat_id:
        return

    env_file = os.path.join(workspace_dir, ".env")
    key = "FEISHU_CURRENT_CHAT_ID"

    try:
        # 读取现有配置
        env_vars = {}
        if os.path.exists(env_file):
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        env_vars[k.strip()] = v.strip()

        # 更新 CHAT_ID
        old_chat_id = env_vars.get(key, "")
        if old_chat_id != chat_id:
            env_vars[key] = chat_id
            # 写回文件
            with open(env_file, 'w', encoding='utf-8') as f:
                f.write("# 自动更新的 chat_id\n")
                for k, v in env_vars.items():
                    f.write(f"{k}={v}\n")
            logger.info(f"已更新工作区 .env 中的 {key}: {chat_id}")
    except Exception as e:
        logger.warning(f"更新工作区 .env 失败: {e}")


def detect_and_prompt_admin_open_id(open_id: str):
    """检测并提示用户设置 admin open_id"""
    global _admin_open_id_detected

    if _admin_open_id_detected:
        return

    current_admin = os.environ.get("FEISHU_MY_ADMIN_OPEN_ID", "").strip()
    if current_admin:
        _admin_open_id_detected = True
        return

    if open_id:
        _admin_open_id_detected = True
        logger.info(f"检测到用户 open_id: {open_id}")
        logger.info("=" * 50)
        logger.info("💡 提示：您可以设置 FEISHU_MY_ADMIN_OPEN_ID 来限制只有您可以触发 Claude")
        logger.info(f"   请在 .env 中添加: FEISHU_MY_ADMIN_OPEN_ID={open_id}")
        logger.info("=" * 50)


def load_workspace_configs() -> List[dict]:
    """从环境变量加载多工作目录配置"""
    global _workspaces

    # 检查是否启用自动发现工作区
    auto_discover = os.environ.get("WORK_DIRS_AUTO_DISCOVER", "").strip().lower()
    if auto_discover in ("1", "true", "yes"):
        # 自动发现：扫描父目录下的所有子目录
        parent_dir = os.environ.get("WORK_DIRS_PARENT_DIR", "").strip()
        if parent_dir and os.path.isdir(parent_dir):
            _workspaces = []
            for entry in os.listdir(parent_dir):
                dir_path = os.path.join(parent_dir, entry)
                if os.path.isdir(dir_path):
                    # 跳过隐藏目录和特殊目录
                    if not entry.startswith('.') and not entry.startswith('_'):
                        _workspaces.append({"name": entry, "path": dir_path})
            if _workspaces:
                logger.info(f"自动发现 {len(_workspaces)} 个工作区:")
                for ws in _workspaces:
                    logger.info(f"  - {ws['name']}: {ws['path']}")
                return _workspaces

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


def switch_workspace(index: int, chat_id: str = None) -> bool:
    """切换到指定索引的工作目录

    Args:
        index: 工作区索引
        chat_id: 可选，指定群聊ID，切换后该群聊将使用此工作区
    """
    global _current_workspace_index
    if 0 <= index < len(_workspaces):
        _current_workspace_index = index
        ws = get_current_workspace()
        logger.info(f"已切换到工作目录: {ws['name']}")

        # 如果提供了 chat_id，更新映射
        if chat_id:
            _workspace_manager.set_chat_workspace(chat_id, index)
            _save_workspace_persist()
            logger.info(f"群聊 {chat_id} 已绑定到工作区 {ws['name']}")

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

# ==================== 多工作区独立进程管理 ====================
class WorkspaceManager:
    """管理多个独立的 Claude Code 进程，每个工作区对应一个进程"""

    def __init__(self):
        self._workspace_senders: Dict[int, ProcessInputSender] = {}  # index -> sender
        self._workspace_pids: Dict[int, int] = {}  # index -> pid
        self._workspace_chat_map: Dict[str, int] = {}  # chat_id -> workspace_index
        self._lock = threading.Lock()

    def ensure_workspace_claude(self, index: int, process_name: str = None) -> Optional[ProcessInputSender]:
        """确保工作区的 Claude Code 进程存在，必要时启动（不等待窗口）"""
        with self._lock:
            # 如果已有 sender，直接返回
            if index in self._workspace_senders:
                sender = self._workspace_senders[index]
                # 检查窗口是否仍然有效
                if sender.find_process_and_window():
                    return sender
                else:
                    # 窗口失效，移除旧的 sender
                    del self._workspace_senders[index]
                    if index in self._workspace_pids:
                        del self._workspace_pids[index]

            # 获取工作区配置
            if index >= len(_workspaces):
                logger.error(f"工作区索引 {index} 超出范围")
                return None

            workspace = _workspaces[index]
            workspace_name = workspace.get("name", f"工作区{index}")

            logger.info(f"启动工作区 {workspace_name} 的 Claude Code...")

            # 启动 Claude Code 并获取 PID
            pid = launch_claude_code(workspace)

            # 保存 PID
            if pid:
                self._workspace_pids[index] = pid
                logger.info(f"工作区 {workspace_name} 的 Claude Code PID: {pid}")

            # 创建新的 sender，传入 PID 用于精确查找窗口
            sender = ProcessInputSender(process_name or PROCESS_NAME, target_pid=pid)
            self._workspace_senders[index] = sender
            logger.info(f"✅ 已启动工作区 {workspace_name} 的 Claude Code，请手动启动窗口或等待其自动启动")
            return sender

    def get_pid(self, index: int) -> Optional[int]:
        """获取工作区的 Claude Code 进程 PID"""
        with self._lock:
            return self._workspace_pids.get(index)

    def get_sender_for_workspace(self, index: int) -> Optional[ProcessInputSender]:
        """获取工作区对应的 sender，不自动启动"""
        with self._lock:
            return self._workspace_senders.get(index)

    def get_or_create_sender(self, index: int) -> Optional[ProcessInputSender]:
        """获取或创建工作区的 sender"""
        sender = self.get_sender_for_workspace(index)
        if sender:
            return sender
        return self.ensure_workspace_claude(index)

    def send_to_workspace(self, index: int, text: str) -> bool:
        """发送消息到指定工作区"""
        sender = self.get_or_create_sender(index)
        if not sender:
            logger.error(f"无法获取工作区 {index} 的 sender")
            return False

        try:
            sender.execute(text)
            return True
        except Exception as e:
            logger.error(f"发送消息到工作区 {index} 失败: {e}")
            return False

    def close_workspace(self, index: int):
        """关闭指定工作区的 Claude Code（仅从管理器中移除，进程由系统管理）"""
        with self._lock:
            if index in self._workspace_senders:
                del self._workspace_senders[index]
                logger.info(f"已关闭工作区 {index} 的 sender")

    def close_all(self):
        """关闭所有工作区"""
        with self._lock:
            self._workspace_senders.clear()
            logger.info("已关闭所有工作区 sender")

    def set_chat_workspace(self, chat_id: str, workspace_index: int):
        """设置群聊对应的工作区"""
        with self._lock:
            self._workspace_chat_map[chat_id] = workspace_index

    def get_chat_workspace(self, chat_id: str) -> int:
        """获取群聊对应的工作区索引"""
        with self._lock:
            # 返回 -1 表示该群聊未绑定工作区
            return self._workspace_chat_map.get(chat_id, -1)

    def is_chat_bound(self, chat_id: str) -> bool:
        """检查群聊是否已绑定工作区"""
        with self._lock:
            return chat_id in self._workspace_chat_map

    def load_persist(self, data: dict):
        """从持久化数据加载"""
        with self._lock:
            chat_map = data.get("workspace_chat_map", {})
            self._workspace_chat_map = {k: int(v) for k, v in chat_map.items()}

    def get_persist_data(self) -> dict:
        """获取需要持久化的数据"""
        with self._lock:
            return {
                "workspace_chat_map": self._workspace_chat_map
            }


# 全局工作区管理器
_workspace_manager = WorkspaceManager()


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

    def __init__(self, process_name: str, target_pid: Optional[int] = None):
        self.process_name = (process_name or "claude.exe").strip().lower()
        self.target_pid = target_pid  # 指定要查找的 Claude 进程 PID
        self.hwnd: Optional[int] = None
        self.pid: Optional[int] = None

    def find_process_and_window(self) -> bool:
        """查找 Claude 进程，并直接使用其父进程（cmd/PowerShell）的窗口"""
        logger.debug(f"[find_process_and_window] target_pid={self.target_pid}")

        # 如果指定了 target_pid，优先用 PID 查找
        if self.target_pid:
            if self._find_by_pid(self.target_pid):
                logger.debug(f"[find_process_and_window] 通过 target_pid={self.target_pid} 找到窗口")
                return True
            logger.debug(f"[find_process_and_window] target_pid={self.target_pid} 查找失败，回退到其他方法")

        # 优先尝试查找 CLI 版本（终端中运行的 claude 命令）
        if self._find_cli_process():
            logger.debug(f"[find_process_and_window] 通过 _find_cli_process 找到窗口")
            return True

        # 其次尝试查找桌面版
        result = self._find_desktop_process()
        logger.debug(f"[find_process_and_window] _find_desktop_process 结果: {result}")
        return result

    def _find_by_pid(self, target_pid: int) -> bool:
        """通过指定的 PID 查找 Claude 进程和窗口"""
        try:
            # 获取 Claude 进程
            proc = psutil.Process(target_pid)
            proc_name = proc.name().lower()

            # 如果是终端进程，直接找窗口
            if proc_name in [n.lower() for n in ProcessInputSender.TERMINAL_PROCESS_NAMES]:
                logger.debug(f"目标 PID 是终端进程: {proc_name}")
                return self._find_terminal_window(target_pid, proc_name)

            # 如果是 claude.exe，找其父进程窗口
            if 'claude' in proc_name:
                parent = proc.parent()
                if parent:
                    parent_name = parent.name().lower()
                    logger.debug(f"Claude 进程的终端: {parent_name}")
                    return self._find_terminal_window(parent.pid, parent_name)

            logger.debug(f"PID {target_pid} 进程名: {proc_name}")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.debug(f"查找 PID {target_pid} 失败: {e}")

        return False

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
        """通过剪贴板粘贴发送（支持中文）。若剪贴板被占用会重试若干次。"""
        if not self.hwnd:
            return

        # 先激活窗口并等待
        self.activate_window()
        time.sleep(0.3)

        # 剪贴板可能被其他进程占用（OpenClipboard 报错 5 拒绝访问），重试几次
        last_err = None
        for attempt in range(5):
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
                if attempt < 4:
                    time.sleep(0.15 * (attempt + 1))
        if last_err is not None:
            logger.warning("剪贴板写入失败（已重试 5 次）: {}，跳过本次注入", last_err)
            return

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
def launch_claude_code(workspace: dict = None) -> Optional[int]:
    """启动 Claude Code（跳过权限确认提示）

    Args:
        workspace: 工作目录信息 {"name": "xxx", "path": "xxx"}，若不传则使用当前工作目录

    Returns:
        启动的进程 PID，失败返回 None
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
        proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        pid = proc.pid
        logger.info(f"✅ Claude Code 已启动 (目录: {workspace_name}, PID: {pid})")
        return pid
    except Exception as e:
        logger.error(f"❌ 启动 Claude Code 失败: {e}")
        return None


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


# ==================== 扩展 WebSocket Client 支持卡片回调 ====================
class ExtendedWSClient(lark_oapi.ws.Client):
    """扩展的 WebSocket Client，支持卡片回调处理
    
    官方 Python SDK (lark_oapi) 的 ws.Client 在 _handle_data_frame 中对 MessageType.CARD
    直接 return，没有实际处理。此类通过重写该方法来添加卡片回调支持。
    """
    
    def __init__(self, app_id: str, app_secret: str, 
                 event_handler=None,
                 card_action_handler=None,
                 log_level=lark_oapi.LogLevel.INFO,
                 domain: str = lark_oapi.core.const.FEISHU_DOMAIN,
                 auto_reconnect: bool = True):
        super().__init__(app_id, app_secret, log_level, event_handler, domain, auto_reconnect)
        self._card_action_handler = card_action_handler
    
    async def _handle_data_frame(self, frame):
        """重写数据帧处理，添加卡片回调支持"""
        import http
        import base64
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.ws.const import HEADER_MESSAGE_ID, HEADER_TRACE_ID, HEADER_SUM, HEADER_SEQ, HEADER_TYPE, HEADER_BIZ_RT
        from lark_oapi.ws.model import Response
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        import time
        
        def _get_by_key(headers, key: str) -> str:
            for header in headers:
                if header.key == key:
                    return header.value
            raise Exception(f"Header not found: {key}")
        
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)
        
        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return
        
        message_type = MessageType(type_)
        logger.debug(f"[ExtendedWSClient] 收到消息, type={message_type.value}, msg_id={msg_id}")
        
        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            result = None
            
            if message_type == MessageType.EVENT:
                if self._event_handler:
                    result = self._event_handler.do_without_validation(pl)
            elif message_type == MessageType.CARD:
                # 处理卡片回调
                if self._card_action_handler:
                    result = self._card_action_handler(pl)
                else:
                    logger.warning(f"收到卡片回调但未注册处理器, msg_id={msg_id}")
                    return
            else:
                return
            
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            logger.error(f"处理消息失败, type={message_type.value}, msg_id={msg_id}, err={e}")
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        
        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())


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




# ==================== 消息处理 ====================
_message_queue = queue.Queue()


def _check_config():
    """检查配置"""
    if not APP_ID or APP_ID == "你的_App_ID":
        logger.error("未配置 FEISHU_APP_ID，请在 .env 中填入飞书凭证")
        sys.exit(1)
    if not APP_SECRET or APP_SECRET == "你的_App_Secret":
        logger.error("未配置 FEISHU_APP_SECRET，请在 .env 中填入飞书凭证")
        sys.exit(1)


def _extract_action_callback_fields(data):
    """提取卡片交互回调的字段 - 支持 SDK 对象和字典两种格式"""
    action = None
    open_id = None
    chat_id = None

    logger.info("_extract_action_callback_fields 收到数据: {}", type(data))

    # 方式1: SDK 对象 (P2CardActionTrigger)
    if hasattr(data, "event"):
        event = data.event
        logger.info("使用 SDK 对象方式解析, event 类型: {}", type(event))

        if hasattr(event, "action") and event.action:
            action_obj = event.action
            # action.value 是一个字典，如 {"action": "switch_workspace", ...}
            action_value = getattr(action_obj, "value", None)
            if isinstance(action_value, dict):
                action = action_value.get("action") or action_value.get("value")
            if not action:
                action = getattr(action_obj, "name", "") or getattr(action_obj, "value", "")

        if hasattr(event, "operator") and event.operator:
            operator = event.operator
            open_id = getattr(operator, "open_id", None) or getattr(operator, "user_id", None)
            logger.info("operator open_id: {}", open_id)

        if hasattr(event, "context") and event.context:
            context = event.context
            chat_id = getattr(context, "open_chat_id", None)
            logger.info("context open_chat_id: {}", chat_id)

    # 方式2: 字典格式
    elif isinstance(data, dict):
        logger.info("使用字典方式解析")
        event = data.get("event", {})
        action_obj = event.get("action", {})
        action_value = action_obj.get("value", {})
        if isinstance(action_value, dict):
            action = action_value.get("action") or action_value.get("value")
        if not action:
            action = action_obj.get("name", "") or action_obj.get("value", "")

        operator = event.get("operator", {})
        open_id = operator.get("open_id") or operator.get("user_id")

        context = event.get("context", {})
        chat_id = context.get("open_chat_id")

    # 处理 action 值 - 支持多种格式
    if isinstance(action, dict):
        # 优先提取 name 字段（工作区切换卡片的格式）
        action = action.get("name") or action.get("action") or action.get("value") or str(action)
    action_text = str(action) if action else "未知操作"

    logger.info("解析结果: action={}, open_id={}, chat_id={}", action_text, open_id, chat_id)
    return action_text, open_id, chat_id


def do_action_callback(data):
    """处理飞书卡片按钮点击回调"""
    logger.info("=" * 50)
    logger.info("收到卡片回调事件 - 开始处理")

    try:
        interaction_text, open_id, chat_id = _extract_action_callback_fields(data)
        logger.info("解析结果 - action: {}, open_id: {}, chat_id: {}", interaction_text, open_id, chat_id)

        if not open_id:
            logger.info("无法解析卡片交互的 open_id，跳过")
            return

        if MY_ADMIN_ID and open_id != MY_ADMIN_ID:
            logger.info(f"非管理员卡片交互已忽略: {open_id}")
            return

        logger.info(f"收到飞书卡片交互: {interaction_text} (open_id: {open_id}, chat_id: {chat_id})")

        # 直接处理工作区切换（不投递到消息队列）
        workspace_name = interaction_text.strip()
        workspaces = load_workspace_configs()
        idx = None
        for i, ws in enumerate(workspaces):
            if ws["name"] == workspace_name:
                idx = i
                break

        if idx is not None:
            if switch_workspace(idx, chat_id):
                ws = get_current_workspace()
                _send_feishu_text(chat_id, f"✅ 已切换到工作目录: **{ws['name']}**\n路径: {ws['path']}")
                # 启动新工作目录的 Claude Code
                _workspace_manager.ensure_workspace_claude(idx)
                logger.info("工作区切换成功: {}", ws["name"])
            else:
                _send_feishu_text(chat_id, f"❌ 切换工作区失败")
        else:
            _send_feishu_text(chat_id, f"❌ 未找到工作区: {workspace_name}")

        logger.info("=" * 50)

    except Exception as e:
        logger.error("处理卡片回调异常: {}", e)
        import traceback
        logger.error("详细堆栈: {}", traceback.format_exc())


def do_process(data):
    """处理飞书消息"""
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

        logger.info(f"收到飞书消息: {user_text[:50]}... (open_id: {open_id}, chat_id: {chat_id})")

        # 处理工作目录切换命令
        user_text_lower = user_text.strip().lower()
        if user_text_lower in ["/切换", "/目录", "/workspace", "/ws"]:
            # 发送工作目录选择卡片
            _send_workspace_selection_card(chat_id, open_id)
            return

        # 处理数字选择切换目录（从卡片点击传来的数字）
        if user_text_lower.isdigit():
            idx = int(user_text_lower) - 1
            if switch_workspace(idx, chat_id):
                ws = get_current_workspace()
                _send_feishu_text(chat_id, f"✅ 已切换到工作目录: **{ws['name']}**\n路径: {ws['path']}")
                # 启动新工作目录的 Claude Code（使用工作区管理器）
                _workspace_manager.ensure_workspace_claude(idx)
            return

        # 直接投递到队列 (包含 open_id 用于后续回复)，不再额外发状态提醒到飞书
        _message_queue.put((user_text, open_id, chat_id))

    except Exception as e:
        logger.error("处理消息异常: {}", e)




def _message_worker():
    """消息处理 worker - 支持多工作区路由"""
    while True:
        try:
            item = _message_queue.get()
            # 支持新版格式: (user_text, open_id, chat_id) 和旧版格式: (user_text, chat_id)
            if isinstance(item, tuple) and len(item) >= 3:
                user_text, open_id, chat_id = item[0], item[1], item[2]
            else:
                user_text, chat_id = item if isinstance(item, tuple) else (item, None)
                open_id = None

            # 确定使用哪个工作区
            logger.info("消息路由调试 - chat_id: {}, _workspace_chat_map: {}",
                       chat_id, _workspace_manager._workspace_chat_map)
            if chat_id:
                workspace_index = _workspace_manager.get_chat_workspace(chat_id)
                logger.info("根据 chat_id 获取的工作区索引: {}", workspace_index)
                # 新群聊未绑定工作区时，提示用户选择
                if workspace_index == -1:
                    _send_feishu_text(chat_id, "👋 您好！这是您首次在此群聊中使用 Claude Code，请先选择一个工作区：")
                    _send_workspace_selection_card(chat_id, open_id)
                    _message_queue.task_done()
                    continue
            else:
                workspace_index = _current_workspace_index
                logger.info("无 chat_id，使用全局工作区索引: {}", workspace_index)

            # 获取工作区信息
            if workspace_index < len(_workspaces):
                workspace_name = _workspaces[workspace_index].get("name", f"工作区{workspace_index}")
            else:
                workspace_name = "默认"

            logger.info(f"正在注入消息到 {workspace_name} (索引: {workspace_index})...")

            # 获取该工作区的 sender
            sender = _workspace_manager.get_or_create_sender(workspace_index)
            if not sender:
                logger.error(f"无法获取工作区 {workspace_name} 的 Claude Code 窗口")
                if chat_id:
                    _send_feishu_text(
                        chat_id,
                        f"❌ 无法连接到工作区 {workspace_name} 的 Claude Code，请确保已启动。"
                    )
                _message_queue.task_done()
                continue

            # 刷新窗口句柄
            if not sender.find_process_and_window():
                logger.error(f"未找到工作区 {workspace_name} 的 Claude Code 窗口")
                if chat_id:
                    _send_feishu_text(
                        chat_id,
                        f"❌ 未找到工作区 {workspace_name} 的 Claude Code 窗口，请先启动或还原。"
                    )
                _message_queue.task_done()
                continue

            # 将当前 chat_id 写入工作区目录的配置文件，供 MCP 工具自动读取
            workspace_dir = _workspaces[workspace_index].get("path", "")
            if workspace_dir and chat_id:
                # 写入 .feishu_current_chat_id 文件
                chat_id_file = os.path.join(workspace_dir, ".feishu_current_chat_id")
                try:
                    with open(chat_id_file, 'w', encoding='utf-8') as f:
                        f.write(chat_id)
                    logger.debug(f"已更新工作区 chat_id 文件: {chat_id_file}")
                except Exception as e:
                    logger.warning(f"写入 chat_id 文件失败: {e}")

                # 同时更新 .env 文件中的 FEISHU_CURRENT_CHAT_ID
                update_workspace_env_chat_id(workspace_dir, chat_id)

            # 检测并提示 admin open_id
            if open_id:
                detect_and_prompt_admin_open_id(open_id)

            # 构造带飞书标记的消息，提示 Claude 使用 feishu-bot MCP 回复
            is_card_interaction = user_text.startswith("【卡片交互】")

            if is_card_interaction:
                # 卡片交互消息
                feishu_marker = f"""【系统提示】此消息来自飞书（卡片交互回调）。
- 当前工作区: {workspace_name}
- 用户已点击卡片按钮，请根据用户的操作继续处理
- 请使用飞书机器人 MCP 工具将结果传回给用户

交互内容：
{user_text}"""
            else:
                # 普通文本消息
                feishu_marker = f"""【系统提示】此消息来自飞书。
- 当前工作区: {workspace_name}
- 请使用飞书机器人 MCP 工具将结果传回给用户

用户消息：
{user_text}"""

            # 执行注入
            sender.execute(feishu_marker)
            logger.info(f"✅ 消息已注入到 {workspace_name}")

            # 定期保存持久化（每10条消息）
            if _message_queue.qsize() % 10 == 0:
                _save_workspace_persist()

            _message_queue.task_done()

        except Exception as e:
            logger.error("消息处理异常: {}", e)
            _message_queue.task_done()


# ==================== 主程序 ====================
def main():
    _check_config()

    logger.info("=" * 50)
    logger.info("飞书 × Claude Code 整合应用")
    logger.info("=" * 50)

    # 0. 加载工作目录配置
    load_workspace_configs()

    # 0.1 加载工作区持久化
    persist_data = _load_workspace_persist()
    if persist_data:
        _workspace_manager.load_persist(persist_data)

    # 1. 启动当前工作区的 Claude Code（使用工作区管理器）
    sender = _workspace_manager.ensure_workspace_claude(_current_workspace_index)
    if not sender:
        logger.warning("启动 Claude Code 失败或等待窗口超时，继续运行...")

    # 2. 启动消息处理 worker
    worker = threading.Thread(target=_message_worker, daemon=True)
    worker.start()

    # 3. 启动飞书 WebSocket
    logger.info("=" * 50)
    logger.info("等待飞书消息中...")
    logger.info("=" * 50 + "\n")

    def _noop(*args, **kwargs):
        pass

    # 卡片回调事件处理器 - 使用 SDK 内置的 register_p2_card_action_trigger 方法
    # SDK 使用 p2.card.action.trigger 作为内部 key
    logger.info("注册卡片回调事件处理器...")
    event_handler = (
        lark_oapi.EventDispatcherHandler.builder(ENCRYPT_KEY, VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(do_process)
        .register_p1_customized_event("im.message.receive_v1", do_process)
        .register_p2_im_message_message_read_v1(_noop)
        .register_p2_im_message_recalled_v1(_noop)
        .register_p2_card_action_trigger(do_action_callback)  # 使用 SDK 内置方法
        .build()  # 重要：需要调用 build() 构建处理器
    )
    logger.info("卡片回调事件处理器注册成功!")

    client = lark_oapi.ws.Client(
        APP_ID, APP_SECRET, event_handler=event_handler, log_level=lark_oapi.LogLevel.INFO
    )
    client.start()


if __name__ == "__main__":
    import signal
    import atexit

    def _cleanup():
        """程序退出时保存持久化"""
        _save_workspace_persist()
        logger.info("已保存工作区持久化数据")

    atexit.register(_cleanup)

    # 捕获 Ctrl+C 信号
    def signal_handler(signum, frame):
        logger.info("收到退出信号，正在保存数据...")
        _save_workspace_persist()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    main()
