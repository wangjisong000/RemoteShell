"""PTY 启动辅助: 关闭 ConPTY 回显后启动 cmd.exe"""
import ctypes
import subprocess
import sys

kernel32 = ctypes.windll.kernel32
STD_INPUT_HANDLE = -10
ENABLE_ECHO_INPUT = 0x0004

handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
if handle and handle != -1:
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        new_mode = mode.value & ~ENABLE_ECHO_INPUT
        kernel32.SetConsoleMode(handle, new_mode)

args = ['cmd.exe']
if len(sys.argv) > 1:
    args.extend(sys.argv[1:])

while True:
    try:
        code = subprocess.call(args)
        sys.exit(code)
    except KeyboardInterrupt:
        pass  # Ctrl+C 穿透给 cmd.exe, wrapper 不退出
