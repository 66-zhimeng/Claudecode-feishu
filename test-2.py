import win32gui
import win32con
import win32api
import win32clipboard
import win32process
import psutil # 需要安装 psutil 来方便地遍历进程
import time
from typing import Optional, List

# 先安装依赖：
# pip install pywin32 psutil

class ProcessInputSender:
    def __init__(self, process_name: str):
        self.process_name = process_name.lower()
        self.hwnd: Optional[int] = None
        self.pid: Optional[int] = None

    def find_process_and_window(self) -> bool:
        """通过进程名查找进程，并获取其主窗口"""
        print(f"🔍 正在查找进程: '{self.process_name}'...")
        
        # 1. 遍历所有进程，找到目标 PID
        target_pids: List[int] = []
        for proc in psutil.process_iter(['name', 'pid']):
            if self.process_name in proc.info['name'].lower():
                target_pids.append(proc.info['pid'])

        if not target_pids:
            print(f"❌ 未找到进程 {self.process_name}")
            return False

        # 通常取第一个找到的，如果你开了多个，可能需要更复杂的逻辑
        self.pid = target_pids[0]
        print(f"✅ 找到进程 PID: {self.pid}")

        # 2. 通过 PID 查找对应的窗口句柄
        def callback(hwnd, _):
            _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
            if found_pid == self.pid and win32gui.IsWindowVisible(hwnd):
                # 找到属于该进程的可见窗口
                self.hwnd = hwnd
                return False
            return True

        print(f"🔍 正在通过 PID {self.pid} 查找窗口...")
        win32gui.EnumWindows(callback, None)

        if self.hwnd:
            print(f"✅ 找到窗口: {win32gui.GetWindowText(self.hwnd)}")
            return True
        else:
            print(f"❌ 找到进程，但未找到可见窗口")
            return False

    def activate_window(self):
        """激活窗口"""
        if not self.hwnd: return
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(self.hwnd)
        time.sleep(0.5)

    def send_text_via_clipboard(self, text: str):
        """通过剪贴板粘贴发送（支持中文）"""
        if not self.hwnd: return
        
        print(f"⌨️  输入内容: {text}")
        
        # 复制到剪贴板
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        win32clipboard.CloseClipboard()
        time.sleep(0.2)

        # 模拟 Ctrl+V
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(ord('V'), 0, 0, 0)
        time.sleep(0.1)
        win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.2)

    def press_enter(self):
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        time.sleep(0.1)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
        print("✅ 已发送回车")

    def execute(self, command: str):
        self.activate_window()
        self.send_text_via_clipboard(command)
        self.press_enter()

# ==========================================
# 使用示例
# ==========================================
if __name__ == "__main__":
    # 1. 配置：进程名
    PROCESS_NAME = "claude.exe"

    # 2. 指令列表
    COMMANDS = [
        "查看当前目录文件",
        "创建一个 test.py"
    ]

    # 3. 初始化
    sender = ProcessInputSender(PROCESS_NAME)

    if not sender.find_process_and_window():
        print("\n💡 请先手动启动 Claude Code，再运行此脚本")
        exit()

    # 4. 倒计时
    print("\n⚠️  即将开始操作，请勿动鼠标键盘...")
    for i in range(3, 0, -1):
        print(i)
        time.sleep(1)

    # 5. 执行
    for cmd in COMMANDS:
        print(f"\n--- 执行指令 ---")
        sender.execute(cmd)
        time.sleep(8) # 等待执行

    print("\n🎉 完成！")