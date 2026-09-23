"""诊断：一次性导入大量曲目时的表现（耗时 / 元数据回填 / 后台线程 / 内存）。

只读工具：不修改配置与会话（跑之前会把它们挪走，结束时原样放回）。
用来复现或排除"添加大文件夹卡顿/闪退"这类问题。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools\\diag_load_folder.py "E:\\Music\\某专辑"
    .venv\\Scripts\\python.exe tools\\diag_load_folder.py "E:\\Music" --seconds 60
"""
import argparse
import atexit
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

_backups = {}
for _rel in (os.path.join(".cache", "session.json"), os.path.join("config", "settings.json")):
    _p = os.path.join(ROOT, _rel)
    if os.path.isfile(_p):
        with open(_p, "rb") as f:
            _backups[_p] = f.read()
        os.remove(_p)


def _restore():
    for _p, _b in _backups.items():
        try:
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            with open(_p, "wb") as f:
                f.write(_b)
        except OSError:
            pass


atexit.register(_restore)


def rss_mb():
    """当前进程工作集（Windows；取不到就返回 0）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                                      ctypes.byref(pmc), pmc.cb)
        return (pmc.WorkingSetSize / 1048576.0) if ok else 0.0
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description="导入大量曲目的性能诊断")
    ap.add_argument("folder", help="要导入的目录")
    ap.add_argument("--seconds", type=float, default=180.0, help="最长观察秒数（默认 180）")
    args = ap.parse_args()

    from PySide6.QtWidgets import QApplication

    qapp = QApplication(sys.argv[:1])
    import main as m
    from app.media_info import scan_folder

    if not os.path.isdir(args.folder):
        print(f"[diag] 目录不存在：{args.folder}")
        return 1

    print(f"[diag] 启动 PlayerApp（rss={rss_mb():.0f}MB）", flush=True)
    p = m.PlayerApp()

    t0 = time.perf_counter()
    paths = scan_folder(args.folder)
    print(f"[diag] 扫描到 {len(paths)} 个音频文件（{time.perf_counter() - t0:.2f}s）", flush=True)
    if not paths:
        print("[diag] 没有可导入的文件")
        return 0

    changed = []
    p.model.changed.connect(lambda: changed.append(1))

    t0 = time.perf_counter()
    p.load_files(paths)
    print(f"[diag] load_files 用时 {time.perf_counter() - t0:.2f}s，"
          f"列表 {len(p.model.tracks)} 首，changed {len(changed)} 次", flush=True)

    t0 = time.perf_counter()
    last = -1
    while time.perf_counter() - t0 < args.seconds:
        qapp.processEvents()
        time.sleep(0.02)
        el = time.perf_counter() - t0
        if int(el) != last:
            last = int(el)
            alive = [w for w in getattr(p, "_probe_workers", []) if not w.isFinished()]
            dur = sum(1 for t in p.model.tracks if t.duration > 0)
            print(f"[diag] t={el:5.1f}s 探测线程={len(alive)} 元数据={dur}/{len(p.model.tracks)} "
                  f"changed={len(changed)} rss={rss_mb():.0f}MB", flush=True)
        if not [w for w in getattr(p, "_probe_workers", []) if not w.isFinished()]:
            end = time.perf_counter() + 0.6
            while time.perf_counter() < end:
                qapp.processEvents()
                time.sleep(0.01)
            break

    dur = sum(1 for t in p.model.tracks if t.duration > 0)
    print(f"[diag] 完成：{len(p.model.tracks)} 首，{dur} 首拿到元数据，"
          f"列表重建 {len(changed)} 次，rss={rss_mb():.0f}MB", flush=True)
    print(f"[diag] 引擎：playing={p.engine.playing} path={p.engine.path}", flush=True)

    p._shutdown_workers()
    p.engine.stop()
    print("[diag] 干净收尾（SURVIVED）", flush=True)
    qapp.quit()
    return 0


if __name__ == "__main__":
    import traceback

    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
