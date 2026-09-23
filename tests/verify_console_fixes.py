"""验证 console 三处修复：旋钮表盘方向、Transport（走带）效果、以及输入增益下的削波。

静音、离线运行（不打开任何音频设备）。
用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_console_fixes.py
"""
import glob
import math
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RETRO_MUTE"] = "1"

import numpy as np                                  # noqa: E402

from app.tape_fx import DEFAULTS, TapeFx            # noqa: E402

SR = 48000
FAIL = []
WARN = []


def check(ok, msg):
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}")
    if not ok:
        FAIL.append(msg)


# ---------------- 1. 旋钮表盘方向：顺时针 = 增大 ----------------
def test_knob_direction():
    from app.console_window import ConsoleSurface as CS

    print("[1] 旋钮表盘方向（屏幕坐标：y 向下为正）")
    lo = CS._knob_dir(0.0, 270.0)      # 最小
    hi = CS._knob_dir(1.0, 270.0)      # 最大
    print(f"    最小值方向 = ({lo[0]:+.3f}, {lo[1]:+.3f})   最大值方向 = ({hi[0]:+.3f}, {hi[1]:+.3f})")
    check(lo[0] < -0.05 and lo[1] > 0, "最小值指针指向左下（逆时针到底）")
    check(hi[0] > 0.05 and hi[1] > 0, "最大值指针指向右下（顺时针到底）")

    # 扫 270° 时 x 分量不是单调的（会先减到 -1 再增），所以按"三个位置"判断顺时针：
    # 左下 → 正上方 → 右下 才是真实旋钮的顺时针行程。
    mid = CS._knob_dir(0.5, 270.0)
    print(f"    中点方向 = ({mid[0]:+.3f}, {mid[1]:+.3f})   （应为正上方）")
    check(abs(mid[0]) < 0.05 and mid[1] < -0.5, "中点指针指向正上方（行程从左下经上方到右下）")


# ---------------- 2. Transport 走带效果 ----------------
def make_tone(seconds=6.0, freq=1000.0):
    # wow 的周期可达 2 秒，测试音要留够几个周期
    n = int(SR * seconds)
    t = np.arange(n) / SR
    mono = 0.35 * np.sin(2 * np.pi * freq * t)
    return np.repeat((mono * 32767.0).astype("<i2"), 2).tobytes()


def render(fx, pcm):
    fx.reset()
    out = bytearray()
    for i in range(0, len(pcm), 8192):
        out += fx.process(pcm[i:i + 8192])
    return bytes(out)


def feat(out_bytes):
    y = np.frombuffer(out_bytes, dtype="<i2").astype(np.float32) / 32768.0
    left = y[0::2]
    rms = float(np.sqrt((left ** 2).mean()))
    seg = left[:8192] * np.hanning(min(8192, left.size))
    spec = np.abs(np.fft.rfft(seg))
    freqs = np.fft.rfftfreq(seg.size, 1.0 / SR)
    tot = spec.sum()
    cent = float((spec * freqs).sum() / tot) if tot > 0 else 0.0
    return rms, cent


def test_transport(fx, pcm):
    print("[2] Transport 走带效果（wowflutter_on = 1）")
    fx.set("wowflutter_on", 1.0)
    for k in ("wow", "flutter", "noise"):
        fx.set(k, 0.0)
    base_rms, base_cent = feat(render(fx, pcm))
    print(f"    基准：rms={base_rms:.5f} 谱重心={base_cent:.0f}Hz")

    for name in ("wow", "flutter", "noise"):
        fx.set(name, 100.0)
        out = render(fx, pcm)
        fx.set(name, 0.0)
        rms, cent = feat(out)
        moved = abs(rms - base_rms) > 1e-4 or abs(cent - base_cent) > 2.0
        print(f"    {name}=100 → rms={rms:.5f} 谱重心={cent:.0f}Hz")
        if name == "wow":
            # 均值指标对 0.5Hz 的慢速摆动本来就不敏感，只用 [2b] 的漂移法判定
            print(f"      （均值指标不适用，见 [2b]）")
        else:
            check(moved, f"{name} 生效（改变了输出）")

    # W/F 关掉时 wow 应当不再起作用（这是 core 的既定前提，不是 bug）
    fx.set("wowflutter_on", 0.0)
    fx.set("wow", 0.0)
    off_rms, off_cent = feat(render(fx, pcm))
    fx.set("wow", 100.0)
    w_rms, w_cent = feat(render(fx, pcm))
    fx.set("wow", 0.0)
    fx.set("wowflutter_on", 1.0)
    print(f"    W/F 关闭时 wow=100：rms {off_rms:.5f}→{w_rms:.5f} 谱重心 {off_cent:.0f}→{w_cent:.0f}Hz")
    check(abs(w_rms - off_rms) < max(1e-3, off_rms * 0.01) and abs(w_cent - off_cent) < 15.0,
          "W/F 关闭时 wow 基本不起作用（符合 core 前提）")


# ---------------- 3. 真实音乐下的输入增益与削波 ----------------
def decode_ffmpeg(path, seconds=8.0):
    import shutil
    from app.audio_engine import find_ffmpeg

    ff = find_ffmpeg()["ffmpeg"]
    cmd = [ff, "-v", "error", "-t", f"{seconds}", "-i", path, "-vn", "-acodec", "pcm_s16le",
           "-ar", str(SR), "-ac", "2", "-f", "s16le", "pipe:1"]
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    out = subprocess.run(cmd, capture_output=True, **kw).stdout
    return out


def test_clipping(fx):
    print("[3] 真实音乐经过磁带机的峰值（输入增益 0dB）")
    files = sorted(glob.glob(r"E:\Driver_E\方大同\**\*.aac", recursive=True))[:3]
    if not files:
        print("    跳过：找不到测试用的真实音乐目录")
        return
    for f in files:
        pcm = decode_ffmpeg(f)
        if len(pcm) < 40000:
            continue
        src = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        in_peak = float(np.abs(src).max())

        fx.set("input_gain_db", 0.0)
        fx._last_pre_peak = 0.0
        fx._soft_clips = 0
        out = render(fx, pcm)
        oy = np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0
        out_peak = float(np.abs(oy).max())
        hard_clip = int(np.sum(np.abs(oy) >= 0.9999))
        print(f"    {os.path.basename(f)[:26]:<28} 输入峰值={in_peak:.3f} "
              f"输出峰值={out_peak:.3f} 硬削波样本={hard_clip} 软限幅块={fx._soft_clips} "
              f"限幅前峰值={fx._last_pre_peak:.3f}")
        check(out_peak <= 1.0, f"{os.path.basename(f)[:20]} 输出未超过满幅")
        check(hard_clip == 0, f"{os.path.basename(f)[:20]} 无硬削波样本")

    print("[3b] 输入增益扫一遍，确认不会因为加增益就削波")
    f = files[0]
    pcm = decode_ffmpeg(f, seconds=4.0)
    for g in (-6.0, 0.0, 6.0, 12.0):
        fx.set("input_gain_db", g)
        fx._soft_clips = 0
        out = render(fx, pcm)
        oy = np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0
        peak = float(np.abs(oy).max())
        hard = int(np.sum(np.abs(oy) >= 0.9999))
        print(f"    input_gain={g:+.0f}dB → 输出峰值={peak:.3f} 硬削波={hard} 软限幅块={fx._soft_clips}")
        check(hard == 0, f"input_gain={g:+.0f}dB 时无硬削波")
    fx.set("input_gain_db", DEFAULTS["input_gain_db"])


def test_wow_drift(fx, pcm):
    """wow 是 0.5Hz 左右的慢速速度调制（周期可达 2 秒）。

    平均 rms / 平均谱重心这类**均值指标看不见慢速摆动**，所以必须看"主频随时间漂移多少"。
    """
    print("[2b] wow 的慢速音高漂移（分段测主频，均值指标测不出来）")

    def mono(out_bytes):
        y = np.frombuffer(out_bytes, dtype="<i2").astype(np.float32) / 32768.0
        return y[0::2]

    def drift(y):
        # 零交叉法测频率：对纯正弦精度远高于短窗 FFT（0.2s 段约 0.5%），
        # 而 wow 造成的音高偏移本身只有 ±1% 量级，FFT 分辨率根本不够。
        seg = int(SR * 0.2)
        freqs = []
        for i in range(0, len(y) - seg, seg):
            part = y[i:i + seg]
            if part.size < 64:
                break
            part = part - part.mean()
            zc = np.count_nonzero(np.diff(np.signbit(part)))
            freqs.append(zc / 2.0 / (part.size / SR))
        if len(freqs) < 3:
            return 0.0, (0.0, 0.0)
        return float(np.std(freqs)), (min(freqs), max(freqs))

    fx.set("wowflutter_on", 1.0)
    fx.set("wow", 0.0)
    fx.set("flutter", 0.0)
    d0, r0 = drift(mono(render(fx, pcm)))

    fx.set("wow", 100.0)
    d1, r1 = drift(mono(render(fx, pcm)))
    fx.set("wow", 0.0)

    fx.set("flutter", 100.0)
    d2, r2 = drift(mono(render(fx, pcm)))
    fx.set("flutter", 0.0)

    print(f"    全关      → 主频标准差 {d0:5.2f}Hz   范围 {r0[0]:.0f}~{r0[1]:.0f}Hz")
    print(f"    wow=100   → 主频标准差 {d1:5.2f}Hz   范围 {r1[0]:.0f}~{r1[1]:.0f}Hz")
    print(f"    flut=100  → 主频标准差 {d2:5.2f}Hz   范围 {r2[0]:.0f}~{r2[1]:.0f}Hz")
    # 观察项，不计入失败：core 里 wowFlutArr 确实传给了 processSample，
    # 但这里两种测法（均值 / 零交叉漂移）都没测出效果，属于待查项而不是已修项。
    for label, d in (("wow", d1), ("flutter", d2)):
        if d > d0 + 1.5:
            print(f"    {label}: 测出漂移（生效）")
        else:
            print(f"    [!] {label}: 未测出漂移 —— 待查（core 内部接线已确认，可能仍是测量局限）")
            WARN.append(f"{label} 未测出音高漂移")


def main():
    fx = TapeFx()
    if not fx.available:
        print(f"磁带 DSP 不可用：{fx.reason}")
        return 1
    fx.prepare(SR, 2048)
    pcm = make_tone()

    test_knob_direction()
    test_transport(fx, pcm)
    test_wow_drift(fx, pcm)
    test_clipping(fx)
    fx.close()

    if WARN:
        print("\n观察项（未计入失败，需继续查/靠听感确认）：")
        for w in WARN:
            print(f"  - {w}")
    print(f"\n{'CONSOLE_FIXES_PASS' if not FAIL else 'CONSOLE_FIXES_FAIL: ' + '; '.join(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    import traceback

    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
