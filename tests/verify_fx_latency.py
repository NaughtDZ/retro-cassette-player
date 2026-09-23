"""实测"参数改动 → 真正生效"的延迟（静音、离线；只用虚拟实时，不碰音频设备）。

修复前：DSP 挂在解码线程，而解码缓冲能有好几秒前瞻——拧旋钮要等缓冲播完才听得到。
修复后：DSP 在投递路径上处理，延迟上限就是一个投递周期。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_fx_latency.py
"""
import atexit
import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RETRO_MUTE"] = "1"          # 静音调试：不出声
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


def main():
    from PySide6.QtWidgets import QApplication

    qapp = QApplication(sys.argv[:1])
    import main as m

    p = m.PlayerApp()
    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    p.model.load_files(paths)
    for t in p.model.tracks:
        p.model.apply_meta(t.path, {"artist": "YG", "album": "A", "duration": 8.0})
    p._play_index(0)

    t0 = time.time()
    while time.time() - t0 < 1.5:               # 让引擎真正跑起来
        qapp.processEvents()
        time.sleep(0.01)

    eng = p.engine
    depth = 0
    with eng._lock:
        depth = sum(len(b) for b in eng._buf)
    print(f"[lat] playing={eng.playing}  解码缓冲深度 = {depth / 192000.0:.2f}s "
          f"(投递前瞻上限 {0.12:.2f}s)")

    if not eng.tape_fx.available:
        print(f"[lat] 磁带 DSP 不可用：{eng.tape_fx.reason}")
        return 1

    fx = eng.tape_fx
    fx.set_to_process_ms = 0.0
    before = fx.set_to_process_ms
    fx.set("input_gain_db", 6.0)                # 改一个会明显改变声音的参数
    t1 = time.time()
    while time.time() - t1 < 1.0:
        qapp.processEvents()
        time.sleep(0.005)
        if fx.set_to_process_ms:
            break

    lat = fx.set_to_process_ms
    assert lat, "参数改动之后没有任何处理发生（DSP 没在投递路径上）"
    print(f"[lat] 参数改动 → 下一次实际处理：{lat:.1f} ms")

    # 再量几次取最坏值
    worst = 0.0
    for v in (0.0, 6.0, 12.0, 0.0):
        fx.set_to_process_ms = 0.0
        fx.set("input_gain_db", v)
        t2 = time.time()
        while time.time() - t2 < 1.0:
            qapp.processEvents()
            time.sleep(0.005)
            if fx.set_to_process_ms:
                break
        worst = max(worst, fx.set_to_process_ms)
    print(f"[lat] 多次改动的最坏延迟：{worst:.1f} ms（旧实现要等缓冲里的音频播完，可达数秒）")

    p.engine.stop()
    p._shutdown_workers()
    print("FX_LATENCY_PASS" if worst < 100.0 else f"FX_LATENCY_FAIL: {worst:.1f} ms")
    qapp.quit()
    return 0 if worst < 100.0 else 1


if __name__ == "__main__":
    import traceback

    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
