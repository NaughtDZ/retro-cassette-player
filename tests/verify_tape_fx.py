"""磁带效果自检：DLL 加载、PCM 往返、参数生效、POWER 开关、实时率。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_tape_fx.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np                                            # noqa: E402

from app.tape_fx import TapeFx                                # noqa: E402

SR = 48000
CHUNK = 8192          # 与音频引擎的读取块一致（2048 帧）


def run_chunks(fx, pcm):
    out = bytearray()
    for i in range(0, len(pcm), CHUNK):
        out += fx.process(pcm[i:i + CHUNK])
    return bytes(out)


def align(y_mono, lag):
    """DSP 有固有延迟：对齐后再和输入比，否则相位差会被误读成"染色"。"""
    return y_mono[lag:] if lag > 0 else y_mono


def thd(x, sr=SR, f0=1000.0, harmonics=(2, 3, 4, 5)):
    """总谐波失真：磁带饱和的直接指标（纯正弦进 → 谐波能量出）。"""
    w = np.hanning(len(x))
    spec = np.abs(np.fft.rfft(x * w))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)

    def band(f, tol=25.0):
        m = (freqs > f - tol) & (freqs < f + tol)
        return float(spec[m].max()) if m.any() else 0.0

    fund = band(f0)
    harm = sum(band(f0 * k) ** 2 for k in harmonics if f0 * k < sr * 0.45) ** 0.5
    return harm / max(fund, 1e-12)


def main():
    fx = TapeFx()
    assert fx.available, f"TapeFx 不可用：{fx.reason}"
    lag = fx.latency_samples()
    print(f"[fx] DLL：{os.path.basename(fx.path)}  固有延迟 {lag} samples ({lag / SR * 1000:.2f} ms)")
    fx.prepare(SR, CHUNK // 4)

    n = SR * 2
    t = np.arange(n + lag) / SR                       # 多喂 lag 帧，便于对齐
    mono = 0.5 * np.sin(2 * np.pi * 1000.0 * t)
    pcm = np.repeat((mono * 32767.0).astype("<i2"), 2).tobytes()

    # ---- 1. 处理链：长度不变、无爆音、对齐后确实被染色 ----
    t0 = time.perf_counter()
    out = run_chunks(fx, pcm)
    ms = (time.perf_counter() - t0) * 1000.0
    assert len(out) == len(pcm), f"PCM 长度变了：{len(out)} vs {len(pcm)}"

    y = np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0
    ref = mono[lag:lag + n]                            # 对齐后的参考输入
    yl = align(y[0::2], lag)[:n]
    diff = float(np.abs(yl - ref).mean())
    peak = float(np.abs(y).max())
    rms_in = float(np.sqrt((ref ** 2).mean()))
    rms_out = float(np.sqrt((yl ** 2).mean()))
    assert diff > 1e-3, f"波形几乎没变（diff={diff:.6f}），DSP 可能没生效"
    assert peak <= 1.0001, f"输出爆表：peak={peak:.4f}"
    assert abs(rms_out - rms_in) / rms_in < 0.35, \
        f"中性参数下电平被改了太多：{rms_in:.4f} → {rms_out:.4f}"
    print(f"[fx] 处理生效（已对齐延迟）：mean|out-in|={diff:.4f}  "
          f"rms {rms_in:.4f}→{rms_out:.4f}  peak={peak:.4f}")
    print(f"[fx] {n / SR:.0f} 秒 48kHz 立体声耗时 {ms:.1f} ms → 实时率 x{(n / SR * 1000) / ms:.1f}")

    # ---- 2. 表头读数跟随输入电平 ----
    loud = np.repeat((0.9 * np.sin(2 * np.pi * 220.0 * t) * 32767.0).astype("<i2"), 2).tobytes()
    run_chunks(fx, loud)
    m = fx.meters()
    assert m["in_peak_l"] > 0.1 or m["vu_l"] > 0.001, f"表头读数全零：{m}"
    print(f"[fx] 表头读数：vuL={m['vu_l']:.4f} vuR={m['vu_r']:.4f} "
          f"inPeakL={m['in_peak_l']:.4f} outPeakL={m['out_peak_l']:.4f}")

    # ---- 3. 提高输入增益 → 谐波失真上升（参数真的进了 DSP）----
    base_thd = thd(align(y[0::2], lag)[:n])
    fx.set("input_gain_db", 18.0)
    hot = run_chunks(fx, pcm)
    hot_y = np.frombuffer(hot, dtype="<i2").astype(np.float32) / 32768.0
    hot_thd = thd(align(hot_y[0::2], lag)[:n])
    assert hot_thd > base_thd * 1.5, f"提高输入增益后 THD 没上升：{hot_thd:.5f} vs {base_thd:.5f}"
    print(f"[fx] 参数生效：input_gain 0→18dB 使 THD {base_thd * 100:.2f}% → {hot_thd * 100:.2f}%")
    fx.set("input_gain_db", 0.0)

    # ---- 4. POWER 关闭时严格直通（字节完全相同）----
    fx.set("active", 0.0)
    off = run_chunks(fx, pcm)
    assert off == pcm, "POWER 关闭时没有严格直通"
    print("[fx] POWER 关闭 → 严格直通（字节一致）")
    fx.set("active", 1.0)

    fx.close()
    print("TAPE_FX_PASS")


if __name__ == "__main__":
    import traceback

    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
