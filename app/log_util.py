"""日志兜底：pythonw（无控制台）启动时 ``sys.stdout`` 是 None。

CPython 对 `print` 在 stdout 为 None 时的处理是**静默丢弃**（不抛异常），所以程序不会
因此崩溃，但也意味着出问题时**一点痕迹都没有**——"添加文件夹闪退"这类故障就只能靠猜。
这里把 stdout/stderr 换成写文件的流：所有既有的 `print` 自动落到
``<项目根>/config/app.log``（软件目录内，不进 git），排查时有据可查。

日志超过 MAX_BYTES 自动轮转成 ``app.log.1``，不会无限增长。
"""
import os
import sys
import threading
import time

MAX_BYTES = 1 << 20          # 1 MB


def log_path(root=None):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "app.log")


class _LogStream:
    """够用的 file-like：只实现 print 需要的 write / flush / isatty。"""

    def __init__(self, path):
        self._path = path
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        if text is None:
            return 0
        with self._lock:
            self._buf += str(text)
            if "\n" not in self._buf:
                return len(str(text))
            lines = self._buf.split("\n")
            self._buf = lines.pop()                  # 最后一段（还没换行）留着
            self._emit(lines)
        return len(str(text))

    def _emit(self, lines):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            if os.path.isfile(self._path) and os.path.getsize(self._path) > MAX_BYTES:
                try:
                    os.replace(self._path, self._path + ".1")
                except OSError:
                    pass
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._path, "a", encoding="utf-8") as f:
                for line in lines:
                    if line.strip():
                        f.write(f"[{stamp}] {line}\n")
        except Exception:
            pass                                      # 日志本身绝不能把程序拖下水

    def flush(self):
        pass

    def isatty(self):
        return False


def install_stdout_fallback(root=None):
    """无控制台时把 stdout/stderr 指向日志文件；有控制台时保持原样（开发时照常看输出）。"""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    stream = _LogStream(log_path(root))
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return stream._path
