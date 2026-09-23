"""Windows 子进程无窗口启动工具。

pythonw 启动的 GUI 进程调用控制台程序（ffmpeg/ffprobe）时，系统会为每个子进程
新建一个控制台窗口 —— 表现为"闪现一堆 cmd 黑框"。加上 CREATE_NO_WINDOW 即可彻底消除。
"""
import os
import subprocess

# CREATE_NO_WINDOW：子进程不创建控制台窗口（仅 Windows 有效）
CREATE_NO_WINDOW = 0x08000000


def hidden_kwargs() -> dict:
    """返回传给 subprocess.run / Popen 的隐藏窗口参数。"""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def run_hidden(cmd, **kwargs):
    """subprocess.run 的无窗口封装。"""
    kw = hidden_kwargs()
    kw.update(kwargs)
    return subprocess.run(cmd, **kw)


def popen_hidden(cmd, **kwargs):
    """subprocess.Popen 的无窗口封装（不覆盖调用方显式传入的 creationflags）。"""
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)
