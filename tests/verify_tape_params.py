"""逐个参数扫描磁带 DSP：每个参数是否真的改变声音？是否会把输出搞坏（NaN / 爆音）？

判据：拿 **默认参数的渲染结果** 当基线，与改了某个参数之后的渲染逐样本比较
（两次都对齐同一固有延迟）。不能拿"输出 vs 原始输入"来判——磁带重放均衡本身
就有相移，那个差值一开始就很大，会把参数带来的真实变化淹没掉。

全程离线、不出声（只调 DSP，不碰音频设备；并强制 RETRO_MUTE）。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_tape_params.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["RETRO_MUTE"] = "1"

import numpy as np                                  # noqa: E402

from app.tape_fx import DEFAULTS, TapeFx            # noqa: E402

SR = 48000
CHUNK = 8192
SECONDS = 1.0

CASES = [
    ("input_gain_db", [-12.0, 12.0, 24.0]),
    ("output_gain_db", [-12.0, 12.0, 24.0]),
    ("bias", [-1.0, -0.5, 0.5, 1.0]),
    ("highpass_hz", [50.0, 200.0, 500.0]),
    ("lowpass_hz", [2000.0, 8000.0, 20000.0]),
    ("noise", [100.0]),
    ("wow", [100.0]),
    ("flutter", [100.0]),
    ("lp_q", [0.5, 1.5, 2.5]),
    ("repro_lf", [-12.0, 12.0]),
    ("repro_lmf", [-12.0, 12.0]),
    ("repro_hmf", [-12.0, 12.0]),
    ("repro_hf", [-12.0, 12.0]),
    ("repro_sub", [-12.0, 12.0]),
    ("level_hmf_trim", [-12.0, 12.0]),
    ("level_hf_trim", [-12.0, 12.0]),
    ("machine", [1.0]),
    ("speed", [0.0, 2.0, 3.0]),
    ("type", [0.0, 2.0, 3.0]),
    ("signal_path", [1.0, 2.0, 3.0]),
    ("eq_standard", [1.0]),
    ("calibration", [0.0, 3.0]),
    ("auto_cal", [0.0]),
    ("oversampling", [0.0, 2.0]),
    ("head_width", [1.0, 2.0]),
    ("crosstalk", [1.0]),
    ("wowflutter_on", [0.0]),
    ("transformer", [0.0]),
    ("auto_comp", [0.0]),
    ("active", [0.0]),
]

CHANGE_EPS = 2e-4


def make_signal(freq=1000.0):
    n = int(SR * SECONDS)
    t = np.arange(n) / SR
    mono = 0.4 * np.sin(2 * np.pi * freq * t)
    return np.repeat((mono * 32767.0).astype("<i2"), 2).tobytes()


def render(fx, pcm):
    out = bytearray()
    for i in range(0, len(pcm), CHUNK):
        out += fx.process(pcm[i:i + CHUNK])
    return bytes(out)


def mono_of(out_bytes, lag):
    y = np.frombuffer(out_bytes, dtype="<i2").astype(np.float32) / 32768.0
    left = y[0::2]
    return left[lag:] if lag else left


def stats(out_bytes, lag, baseline=None):
    y = np.frombuffer(out_bytes, dtype="<i2").astype(np.float32) / 32768.0
    left = y[0::2]
    finite = bool(np.isfinite(left).all())
    peak = float(np.abs(y).max()) if left.size else 0.0
    rms = float(np.sqrt((left ** 2).mean())) if left.size else 0.0
    # 谱重心：与相位无关的"音色"特征。逐样本差值受延迟/相位影响极大
    # （磁带重放均衡本身就有相移），用它判断"参数是否改变声音"会误判。
    centroid = 0.0
    if left.size > 4096:
        seg = left[:8192] * np.hanning(min(8192, left.size))
        spec = np.abs(np.fft.rfft(seg))
        freqs = np.fft.rfftfreq(seg.size, 1.0 / SR)
        tot = spec.sum()
        centroid = float((spec * freqs).sum() / tot) if tot > 0 else 0.0
    delta = -1.0
    if baseline is not None:
        a = mono_of(out_bytes, lag)
        n = min(len(a), len(baseline))
        delta = float(np.abs(a[:n] - baseline[:n]).mean()) if n else -1.0
    return finite, peak, rms, delta, centroid


def main():
    fx = TapeFx()
    if not fx.available:
        print(f"[params] tape DSP unavailable: {fx.reason}")
        return 1
    fx.prepare(SR, CHUNK // 4)      # ★ 必须 prepare，否则 shim 直接透传，所有参数都"没效果"
    lag = fx.latency_samples()
    print(f"[params] DSP ready, inherent latency {lag} samples (aligned in analysis)")

    # 关掉随机调制：wow/flutter/noise 每次渲染的波形都不同，会让逐样本差值失去意义
    for k in ("wow", "flutter", "noise"):
        fx.set(k, 0.0)
    fx.set("wowflutter_on", 0.0)

    pcm = make_signal()

    def render_clean():
        fx.reset()                  # 每次从干净的滤波器/延迟线状态出发，两次渲染只差那个参数
        return render(fx, pcm)

    ref_out = render_clean()
    ref_y = mono_of(ref_out, lag)
    _, ref_peak, ref_rms, _, ref_cent = stats(ref_out, lag)
    print(f"[params] baseline (defaults, random sources off): rms={ref_rms:.5f} "
          f"peak={ref_peak:.5f} centroid={ref_cent:.0f}Hz\n")

    print(f"{'parameter':<17}{'value':>9}{'affects':>9}{'rms':>10}{'peak':>9}{'centroid':>10}   note")
    print("-" * 74)

    bad, inert = [], []
    for name, values in CASES:
        for v in values:
            fx.set(name, v)
            out = render_clean()
            fx.set(name, DEFAULTS.get(name, 0.0))
            finite, peak, rms, delta, cent = stats(out, lag, ref_y)

            notes = []
            if not finite:
                notes.append("NON-FINITE(NaN/Inf)")
            if peak > 1.0:
                notes.append(f"CLIP peak={peak:.3f}")
            if rms < 1e-5 and ref_rms > 1e-3:
                notes.append("SILENT")
            # 判据：电平或音色变了才算"这个参数有作用"（相位无关）
            affects = (abs(rms - ref_rms) > 1e-4 * max(1.0, ref_rms)
                       or abs(cent - ref_cent) > max(5.0, 0.01 * ref_cent))
            if name == "active":                       # POWER 关闭应字节直通
                if out != pcm:
                    notes.append("POWER off is NOT byte-exact passthrough")
                else:
                    notes.append("byte-exact passthrough OK")
            flag = "; ".join(notes) if notes else "ok"
            print(f"{name:<17}{v:>9.2f}{('yes' if affects else 'no'):>9}"
                  f"{rms:>10.5f}{peak:>9.4f}{cent:>10.0f}   {flag}")
            if notes and "byte-exact passthrough OK" not in "; ".join(notes):
                bad.append((name, v, notes))
            elif not affects and name != "active":
                inert.append((name, v))

    print("-" * 74)
    if bad:
        print(f"\n[params] {len(bad)} settings corrupted the output:")
        for name, v, notes in bad:
            print(f"  - {name}={v}: {'; '.join(notes)}")
    else:
        print("\n[params] no setting corrupted the output (no NaN, no clipping, no accidental silence)")

    if inert:
        print(f"\n[params] {len(inert)} settings had no measurable effect:")
        for name, v in inert:
            print(f"  - {name}={v}")

    # ---- bias 的专项验证：AUTO CAL 开着时它被自动校准覆盖，关掉后才生效 ----
    fx.set("auto_cal", 1.0)
    fx.set("bias", 0.2)
    with_auto = render_clean()
    fx.set("auto_cal", 0.0)
    fx.set("bias", 0.2)
    manual_a = render_clean()
    fx.set("bias", 0.8)
    manual_b = render_clean()
    a_y, b_y = mono_of(manual_a, lag), mono_of(manual_b, lag)
    _, _, rms_a, _, cent_a = stats(manual_a, lag)
    _, _, rms_b, _, cent_b = stats(manual_b, lag)
    bias_changed = abs(rms_a - rms_b) > 1e-4 or abs(cent_a - cent_b) > 5.0
    print(f"\n[params] bias 专项：手动模式下 bias 0.2 vs 0.8 → rms {rms_a:.5f}/{rms_b:.5f}，"
          f"谱重心 {cent_a:.0f}/{cent_b:.0f} Hz → {'有效' if bias_changed else '无效！'}")
    auto_y = mono_of(with_auto, lag)
    n = min(len(auto_y), len(b_y))
    auto_overrides = (with_auto == manual_b) or (
        n > 0 and float(np.abs(auto_y[:n] - b_y[:n]).mean()) > 0.01)
    print(f"[params] bias 专项：AUTO CAL 开着时手感值被自动校准覆盖 = {auto_overrides}")
    fx.set("auto_cal", 1.0)
    fx.set("bias", 0.5)

    fx.close()
    print("\nPARAM_SCAN_DONE")
    return 0 if not bad else 2


if __name__ == "__main__":
    import traceback

    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
