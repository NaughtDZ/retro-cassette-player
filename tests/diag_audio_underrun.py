"""诊断：真实音频输出链路的欠载（爆音/白噪音的常见根因）。

用**真实音频设备**跑，但把音量设为 0（不出声到耳机）。监听：
- 当前后端与格式、缓冲大小（用户问的"缓冲区设置"）
- QAudioSink 状态切换（IdleState = 缓冲被抽空 → 欠载 → 爆音/咔哒）
- 投递周期与单次投递耗时（我在 GUI 线程里做 DSP，卡一下就可能欠载）

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\diag_audio_underrun.py
"""
import atexit
import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
# 故意不设 offscreen：要真实设备才能观察欠载；音量为 0 保证不出声
os.environ.pop("RETRO_MUTE", None)

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
    from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices
    from PySide6.QtWidgets import QApplication

    qapp = QApplication(sys.argv[:1])

    dev = QMediaDevices.defaultAudioOutput()
    print(f"[line] 默认输出设备：{dev.description()!r}")
    print(f"[line] Qt 后端由 QAudioSink 内部选择（Windows 上 Qt6 用 WASAPI 共享模式；"
          f"可用 QT_MULTIMEDIA_PREFERRED_PLUGINS 覆盖）")

    import main as m

    p = m.PlayerApp()
    eng = p.engine
    sink = eng._sink
    if sink is None:
        print("[line] 没有真实音频设备（engine._sink 为 None）→ 无法观测")
        return 0

    sink.setVolume(0.0)                     # 静音：链路照跑，声音不进耳机
    print(f"[line] sink 格式：{sink.format().sampleRate()}Hz "
          f"{sink.format().channelCount()}ch {sink.format().sampleFormat()}")
    print(f"[line] bufferSize = {sink.bufferSize()} bytes "
          f"({sink.bufferSize() / 192000.0 * 1000:.0f} ms)")
    print(f"[line] 已把 sink 音量置 0（本脚本不出声）")

    events = []
    sink.stateChanged.connect(lambda st: events.append((st, time.monotonic())))

    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    p.model.load_files(paths)
    for t in p.model.tracks:
        p.model.apply_meta(t.path, {"artist": "YG", "album": "A", "duration": 8.0})
    p._play_index(0)

    # 每 10ms 采样一次；★ 关键指标是"设备实际消费了多少音频"（processedUSecs），
    # 这才是"有没有声音"的客观证据。bytesFree / stateChanged 都证明不了这一点。
    samples = []
    starve = []
    t0 = time.time()
    stall_at = t0 + 2.5
    stalled = False
    us_start = None
    while time.time() - t0 < 6.0:
        qapp.processEvents()
        if us_start is None:
            us_start = sink.processedUSecs()
        free = sink.bytesFree()
        samples.append(free)
        if free == 0:
            starve.append(time.time() - t0)
        if not stalled and time.time() > stall_at:
            stalled = True
            t_stall = time.monotonic()
            t_end = t_stall + 0.25
            while time.monotonic() < t_end:      # 死等 250ms，完全不投递
                pass
            print(f"[line] 已人为卡顿 {(time.monotonic() - t_stall) * 1000:.0f} ms（模拟打开面板）")
        time.sleep(0.01)

    played = (sink.processedUSecs() - us_start) / 1e6
    reads = eng._source.reads if eng._source is not None else 0
    # 注意：QAudio.State 枚举在 PySide6 里 == 比较不可靠，比 value
    idle = [t for st, t in events if int(getattr(st, "value", -1)) == int(QAudio.State.IdleState.value)]
    print(f"[line] ★ 设备实际播放时长 = {played:.2f}s（<=0 就等于没有声音）")
    print(f"[line] 源被拉取次数 = {reads}；pull 模式 = {eng._pull}")
    print(f"[line] sink 状态切换次数：{len(events)}；其中 Idle（缓冲被抽空/欠载）{len(idle)}")
    print(f"[line] bytesFree==0 的采样数：{len(starve)}/{len(samples)}")
    print(f"[line] 播放中 playing={eng.playing}，DSP 输出异常次数={eng.tape_fx._glitches}")

    p.engine.stop()
    p._shutdown_workers()
    qapp.quit()
    print("DIAG_LINE_PASS" if played > 2.0 and reads > 0 else
          f"DIAG_LINE_FAIL: played={played:.2f}s reads={reads} pull={eng._pull}")
    return 0 if (played > 2.0 and reads > 0) else 1


if __name__ == "__main__":
    import traceback

    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
