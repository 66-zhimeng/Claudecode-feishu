import subprocess
import os
import sys

# 路径配置
CLAUDE_PATH = r"C:\Users\yq\.local\bin\claude.exe"
WORK_DIR = r"D:\ceshi_python\Claudecode-feishu"

# 输出当前进程名和编号
_process_name = os.path.basename(sys.executable)
_process_id = os.getpid()
print(f"当前进程名: {_process_name}")
print(f"当前进程编号 (PID): {_process_id}")
print("✅ 正在启动 Claude Code...")

# 切换到工作目录
os.chdir(WORK_DIR)

# 核心：直接在当前控制台启动，不做任何管道重定向
# 这样 Python 只是个启动器，启动后你直接和 Claude Code 交互
try:
    subprocess.run(
        [CLAUDE_PATH],
        check=True,
        creationflags=subprocess.CREATE_NEW_CONSOLE # 这一行可选：如果你想弹出一个新窗口用，就加上这行
    )
except KeyboardInterrupt:
    pass
except Exception as e:
    print(f"启动失败: {e}")

print("\n👋 程序结束")