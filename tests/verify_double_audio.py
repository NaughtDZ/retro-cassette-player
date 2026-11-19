"""解码进程自检：「启动预载 → 点播放」只允许有一个 ffmpeg 在解码。

回归的 bug（v1.2.0 及以前）：
  启动时 _preload() 用 load(start_playing=False) 把上次的曲目装进引擎，点播放走
  resume()。旧实现里 resume() 直接 _spawn() 起新进程，却没掐掉预载留下的那个——
  两个 ffmpeg、两个读线程同时往 self._buf 里灌两条 PCM 流，混在一起就是
  两个音轨 / 爆音 / 严重卡顿。附带两个问题也在这里守住：
  · 预载时 playing=False 绕过了背压，整首曲子会无上限灌进内存；
  · 退出时没人 kill 解码进程，ffmpeg 在后台残留。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_double_audio.py
"""
import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RETRO_MUTE"] = "1"           # 强制不打开音频设备（虚拟实时推进）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication                     # noqa: E402

from app.audio_engine import (AudioEngine, BUFFER_CAP_SECONDS,  # noqa: E402
                              BYTES_PER_SEC, find_ffmpeg)

BAD = []


def check(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        BAD.append(msg)


def main():
    qapp = QApplication(sys.argv)
    ff = find_ffmpeg(os.path.join(ROOT, "tools", "ffmpeg"))

    # 10 分钟测试音：短样本解码太快（0.2s 就 EOF），复现不出"两个进程同时在解"
    tmp = os.path.join(tempfile.gettempdir(), "retro_double_audio_test.mp3")
    subprocess.run([ff["ffmpeg"], "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=600",
                    "-ac", "2", "-q:a", "9", "-y", tmp], check=True)

    eng = AudioEngine(ff["dir"])

    def pump(sec):
        t = time.time()
        while time.time() - t < sec:
            qapp.processEvents()
            time.sleep(0.02)

    def buf_seconds():
        with eng._lock:
            return sum(len(b) for b in eng._buf) / BYTES_PER_SEC

    try:
        # ---------- 1. 预载：不出声、背压生效、进程待命 ----------
        eng.load(tmp, start_playing=False, offset=0.0)
        proc0 = eng._proc
        check(not eng.playing, "预载后 playing=False（启动不出声）")
        check(proc0 is not None and proc0.poll() is None, "预载后解码进程已就绪")
        pump(0.6)
        b = buf_seconds()
        check(b <= BUFFER_CAP_SECONDS + 1.0,
              f"预载缓冲受背压限制：{b:.2f}s ≤ {BUFFER_CAP_SECONDS}s+1")

        # ---------- 2. 点播放（resume）：只能剩一个解码进程 ----------
        eng.resume()
        pump(1.0)
        new_proc = eng._proc
        check(proc0.poll() is not None, "resume 后旧解码进程已被掐掉")
        check(new_proc is not None and new_proc.pid != proc0.pid,
              "resume 起了新的解码进程")
        check(eng.playing, "resume 后 playing=True")

        # 单流判据：虚拟实时下每秒只该解出约 1 秒 PCM（两条流会接近 2×）
        with eng._lock:
            d0 = eng._decoded
        t0 = time.monotonic()
        pump(1.0)
        with eng._lock:
            d1 = eng._decoded
        rate = (d1 - d0) / BYTES_PER_SEC / max(1e-6, time.monotonic() - t0)
        check(rate < 1.8, f"缓冲里只有一条流在灌（{rate:.2f}× 实时，双流会≈2×）")

        # ---------- 3. 位置按实时推进（虚拟模式） ----------
        p0 = eng.position()
        pump(0.8)
        p1 = eng.position()
        check(0.4 < (p1 - p0) < 1.6, f"播放位置按实时推进（{p0:.2f}s → {p1:.2f}s）")

        # ---------- 4. 退出清理：不留 ffmpeg ----------
        eng.shutdown()
        pump(0.4)
        check(new_proc.poll() is not None, "shutdown 后解码进程已退出")
    finally:
        try:
            eng.shutdown()
        except Exception:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass

    print()
    if BAD:
        print(f"FAILED {len(BAD)} 项：")
        for m in BAD:
            print("  - " + m)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
