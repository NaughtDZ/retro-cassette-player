"""磁带效果：Dusk TapeMachine DSP 核心的 Python 绑定（DSP 的唯一入口层）。

- DLL 由 ``tools/build_dusk_dsp.ps1`` 编出，源码在 ``third_party/dusk_ctm``
  （上游 dusk-audio-plugins 的框架无关 TapeMachine core，GPL-3.0-or-later）。
- **找不到 DLL 或 numpy 时整体退化为"直通"**：``available`` 为 False，
  ``process()`` 原样返回数据，绝不报错、绝不把声音变成静音。
- 许可隔离：所有 DSP 调用都收在本文件里。将来若不想让 GPL 代码进进程
  （例如要对外分发），把这一层换成子进程管线或自研实现即可，上层无需改动。

音频格式固定为 **交错 s16le / 48kHz / 立体声**，与音频引擎的 PCM 管道一致。
"""
from __future__ import annotations

import ctypes
import os
import time

# ---- 参数 id：必须与 third_party/dusk_ctm/dusk_dsp_shim.cpp 的枚举逐项一致 ----
PARAMS = {
    "machine": 0, "speed": 1, "type": 2, "signal_path": 3, "eq_standard": 4,
    "input_gain_db": 5, "bias": 6, "calibration": 7, "auto_cal": 8,
    "highpass_hz": 9, "lowpass_hz": 10, "noise": 11, "wow": 12, "flutter": 13,
    "output_gain_db": 14, "auto_comp": 15, "oversampling": 16, "head_width": 17,
    "crosstalk": 18, "wowflutter_on": 19, "transformer": 20,
    "repro_lf": 21, "repro_lmf": 22, "repro_hmf": 23, "repro_hf": 24, "repro_sub": 25,
    "level_hmf_trim": 26, "level_hf_trim": 27, "lp_q": 28, "bypass": 29,
    "active": 30,
}
METERS = {
    "vu_l": 100, "vu_r": 101,
    "in_peak_l": 102, "in_peak_r": 103,
    "out_peak_l": 104, "out_peak_r": 105,
}
INFO_LATENCY = 200

# 默认＝"淡淡一层磁带味"：不过载、不抖、无噪声；POWER 默认开
DEFAULTS = {
    "active": 1.0,
    "machine": 0.0, "speed": 1.0, "type": 1.0, "signal_path": 0.0, "eq_standard": 0.0,
    # bias 是 0..1 的归一化偏磁量（0.5 = 中性）：core 内部按 (biasAmount - 0.5) 计算，
    # 而且只有 auto_cal 关闭（手动校准）时它才真正起作用。
    "input_gain_db": 0.0, "bias": 0.5, "calibration": 0.0, "auto_cal": 1.0,
    "highpass_hz": 20.0, "lowpass_hz": 20000.0,
    "noise": 0.0, "wow": 0.0, "flutter": 0.0,
    "output_gain_db": 0.0, "auto_comp": 1.0, "oversampling": 1.0,
    "head_width": 1.0,            # core 默认 1（1/2 英寸）；且仅 American 机型下有效
    "crosstalk": 0.0, "wowflutter_on": 1.0, "transformer": 1.0,
    "repro_lf": 0.0, "repro_lmf": 0.0, "repro_hmf": 0.0, "repro_hf": 0.0, "repro_sub": 0.0,
    "level_hmf_trim": 0.0, "level_hf_trim": 0.0, "lp_q": 0.707, "bypass": 0.0,
}

# 参数的中文标签与范围，供 console 面板生成控件（min, max, step, 是否整数）
PARAM_SPEC = {
    "input_gain_db":  ("驱动 DRIVE", -24.0, 24.0, 0.5, False, "dB"),
    "output_gain_db": ("输出增益", -24.0, 24.0, 0.5, False, "dB"),
    "bias":           ("偏磁", 0.0, 1.0, 0.01, False, ""),   # 0..1，0.5 中性（需关 AUTO CAL 才生效）
    "noise":          ("磁带噪声", 0.0, 100.0, 1.0, False, "%"),
    "wow":            ("抖晃 Wow", 0.0, 100.0, 1.0, False, "%"),
    "flutter":        ("抖动 Flutter", 0.0, 100.0, 1.0, False, "%"),
    "highpass_hz":    ("高通", 20.0, 500.0, 5.0, False, "Hz"),
    "lowpass_hz":     ("低通", 2000.0, 20000.0, 100.0, False, "Hz"),
    "repro_lf":       ("重放 LF", -12.0, 12.0, 0.5, False, "dB"),
    "repro_lmf":      ("重放 LMF", -12.0, 12.0, 0.5, False, "dB"),
    "repro_hmf":      ("重放 HMF", -12.0, 12.0, 0.5, False, "dB"),
    "repro_hf":       ("重放 HF", -12.0, 12.0, 0.5, False, "dB"),
    "repro_sub":      ("重放超低", -12.0, 12.0, 0.5, False, "dB"),
    "lp_q":           ("低通 Q", 0.5, 2.5, 0.01, False, ""),
}
# 离散挡位（值 → 显示文字）
ENUMS = {
    "machine":    ["Swiss 瑞士机", "American 美国机"],
    "speed":      ["3.75 IPS", "7.5 IPS", "15 IPS", "30 IPS"],
    "type":       ["250", "456", "GP9", "900"],
    "signal_path": ["Repro", "Sync", "Input", "Thru"],
    "eq_standard": ["NAB", "CCIR"],
    "calibration": ["+6", "+3", "0", "-3"],
    "oversampling": ["关闭", "2x", "4x"],
    "head_width": ["标准", "宽", "窄"],
}


OUTPUT_BUFFER_SECONDS = 0.12    # 真实输出缓冲（audio_engine 用）
# core 的 wow/flutter 深度是按真实磁带标定的：面板拧到 100% 也**只有约 ±0.5% 的速度变化**
# （实测 1kHz 在 995~1000Hz 之间漂移，约 8 音分），在音乐上基本听不出来。
# 给一个放大系数，让拧到底时是"明显但不夸张"的抖晃（约 ±1.5%），这是听感取向，不是修 core。
WOW_FLUTTER_GAIN = 3.0


def _harden(name, value):
    """把面板值转换成真正送给 DSP 的值。"""
    if name in ("wow", "flutter"):
        return float(value) * WOW_FLUTTER_GAIN
    return value


# 分区级开关：关掉某个分区 = 把它的参数临时置成"中性值"（记住原值，开回来时恢复）。
# 这不是 DSP 内的 bypass（core 只有整体 bypass），而是参数域上的等效做法：
# input 的增益/偏置归零、transport 的抖晃噪声归零、output 的增益补偿归零、重放 EQ 全平。
MODULE_NEUTRAL = {
    "input":     {"input_gain_db": 0.0, "bias": 0.5, "highpass_hz": 20.0, "lowpass_hz": 20000.0},
    "transport": {"wow": 0.0, "flutter": 0.0, "noise": 0.0, "wowflutter_on": 0.0},
    "output":    {"output_gain_db": 0.0, "auto_comp": 0.0},
    "repro_eq":  {"repro_lf": 0.0, "repro_lmf": 0.0, "repro_hmf": 0.0,
                  "repro_hf": 0.0, "repro_sub": 0.0},
}


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _try_numpy():
    try:
        import numpy
        return numpy
    except Exception:
        return None


class TapeFx:
    """磁带 DSP 的薄封装；不可用时就是一条直通线。"""

    def __init__(self, dll_path=None):
        self.available = False
        self.path = None
        self.reason = ""
        self.params = dict(DEFAULTS)
        self.modules = {k: True for k in MODULE_NEUTRAL}   # 分区开关状态
        self._module_saved = {}                            # 分区关闭时记住的用户值
        self._glitches = 0                                 # DSP 输出异常次数（写日志用）
        self._soft_clips = 0                               # 触发软限幅的块数（写日志用）
        self._last_pre_peak = 0.0                          # 软限幅前的最高峰值（诊断用）
        self._safety_gain = 1.0                            # 防持续过载的慢速安全增益
        self._pending_set_t = 0.0                          # 最近一次参数变更时刻（探针）
        self.set_to_process_ms = 0.0                       # 实测：变更到下一次处理的毫秒数
        self._np = _try_numpy()
        self._dll = None
        self._handle = None
        self._dst = None
        self._prepared = None

        if self._np is None:
            self.reason = "缺少 numpy（pip install numpy），磁带效果已停用"
            print(f"[tape_fx] {self.reason}")
            return

        dll = dll_path or os.path.join(_project_root(), "tools", "dusk", "dusk_dsp.dll")
        if not os.path.isfile(dll):
            self.reason = f"未找到 {dll}（跑 tools\\build_dusk_dsp.ps1 生成），磁带效果已停用"
            print(f"[tape_fx] {self.reason}")
            return
        try:
            lib = ctypes.CDLL(dll)
            lib.dusk_create.restype = ctypes.c_void_p
            lib.dusk_destroy.argtypes = [ctypes.c_void_p]
            lib.dusk_prepare.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_int]
            lib.dusk_reset.argtypes = [ctypes.c_void_p]
            lib.dusk_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_float]
            lib.dusk_get.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.dusk_get.restype = ctypes.c_float
            lib.dusk_latency.argtypes = [ctypes.c_void_p]
            lib.dusk_latency.restype = ctypes.c_int
            lib.dusk_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.c_int]
            lib.dusk_process.restype = None
            handle = lib.dusk_create()
            if not handle:
                raise RuntimeError("dusk_create 返回空句柄")
            self._dll, self._handle, self.path = lib, handle, dll
            self.available = True
            for k, v in self.params.items():
                self._apply(k, v)
        except Exception as e:
            self.reason = f"加载 {dll} 失败：{e}；磁带效果已停用"
            print(f"[tape_fx] {self.reason}")

    # ---------------- 生命周期 ----------------
    def prepare(self, sample_rate, max_block):
        """按采样率/块大小准备 DSP（内部会分配缓冲，只在换曲/换设备时调用）。"""
        if not self.available:
            return
        max_block = max(64, int(max_block))
        if self._prepared == (sample_rate, max_block):
            return
        try:
            self._dll.dusk_prepare(self._handle, float(sample_rate), max_block)
            self._prepared = (sample_rate, max_block)
        except Exception as e:
            print(f"[tape_fx] prepare 失败：{e}")
            self.available = False

    def reset(self):
        if self.available:
            try:
                self._dll.dusk_reset(self._handle)
            except Exception:
                pass

    def close(self):
        if self._dll is not None and self._handle:
            try:
                self._dll.dusk_destroy(ctypes.c_void_p(self._handle))
            except Exception:
                pass
            self._handle = None
        self.available = False

    # ---------------- 参数 ----------------
    def _apply(self, name, value):
        pid = PARAMS.get(name)
        if pid is None:
            return
        try:
            self._dll.dusk_set(self._handle, pid, float(_harden(name, value)))
        except Exception:
            pass

    def set(self, name, value):
        """设置一个参数（立即生效，DSP 内部是 atomic 存储）。"""
        if name not in PARAMS:
            return
        self.params[name] = float(value)
        if self.available:
            self._apply(name, value)
        self._pending_set_t = time.monotonic()      # 探针：量"改动 → 下一次真正生效"的延迟

    def set_many(self, mapping):
        for k, v in (mapping or {}).items():
            self.set(k, v)

    def get(self, name):
        return self.params.get(name, 0.0)

    # ---------------- 分区开关（一键 bypass 某个效果区块） ----------------
    def set_module(self, name, enabled):
        """关掉某分区 = 把它的参数临时置中性，记住原值；开回来时恢复。

        注意：这不是 DSP 内的 bypass（core 只提供整体 bypass），而是参数域上的等效做法。
        想要真正一声不染，用整体 POWER（``set("active", 0)``）。
        """
        enabled = bool(enabled)
        if enabled == bool(self.modules.get(name, True)):
            return
        neutral = MODULE_NEUTRAL.get(name)
        self.modules[name] = enabled
        if not neutral or not self.available:
            return
        if enabled:
            for k, v in (self._module_saved.pop(name, {}) or {}).items():
                self.set(k, v)
        else:
            self._module_saved[name] = {k: self.params.get(k, v) for k, v in neutral.items()}
            for k, v in neutral.items():
                self.set(k, v)

    def module_enabled(self, name):
        return bool(self.modules.get(name, True))

    def export_params(self):
        """导出"用户视角"的参数：分区关着时给出被记住的原值，而不是中性值。"""
        out = dict(self.params)
        for _name, saved in self._module_saved.items():
            out.update(saved)
        return out

    def latency_samples(self):
        if not self.available:
            return 0
        try:
            return int(self._dll.dusk_latency(self._handle))
        except Exception:
            return 0

    # ---------------- 表头读数 ----------------
    def meters(self):
        """返回 {'vu_l','vu_r','in_peak_l',...}；DSP 不可用时全是 0。"""
        if not self.available:
            return {k: 0.0 for k in METERS}
        out = {}
        for k, pid in METERS.items():
            try:
                out[k] = float(self._dll.dusk_get(self._handle, pid))
            except Exception:
                out[k] = 0.0
        return out

    # ---------------- 音频处理 ----------------
    def process(self, data: bytes) -> bytes:
        """交错 s16le 进、交错 s16le 出；不可用或未启用时原样返回。"""
        if not self.available or len(data) < 8:
            return data
        if self._pending_set_t:
            self.set_to_process_ms = (time.monotonic() - self._pending_set_t) * 1000.0
            self._pending_set_t = 0.0
        if self.params.get("active", 1.0) <= 0.5:
            return data
        np = self._np
        frames = len(data) // 4                      # 2 通道 × 2 字节
        if frames <= 0:
            return data
        src = np.frombuffer(data, dtype="<i2").astype(np.float32)
        src *= 1.0 / 32768.0
        if self._dst is None or self._dst.size < frames * 2:
            self._dst = np.zeros(frames * 2, dtype=np.float32)
        dst = self._dst[: frames * 2]
        try:
            self._dll.dusk_process(self._handle, src.ctypes.data, dst.ctypes.data, frames, 2)
        except Exception as e:
            print(f"[tape_fx] process 失败，后续直通：{e}")
            self.available = False
            return data
        # 输出保护：非有限值或异常爆表一律退回原始 PCM——宁可没有染色，
        # 也绝不让 NaN / 巨大噪声传到耳朵里。
        if not np.isfinite(dst).all() or float(np.abs(dst).max()) > 4.0:
            self._glitches += 1
            if self._glitches in (1, 10, 100):
                print(f"[tape_fx] DSP 输出异常（第 {self._glitches} 次），已退回原始音频")
            return data
        # 软限幅：磁带重放均衡与磁滞在高电平处会有明显过冲（实测输入已满幅的真实音乐
        # 会被推到 1.82），直接硬 clip 就是刺耳的削波。用 0.98*tanh(x/0.98)：
        # 小信号几乎完全线性（|x|<0.3 时误差 <1%），只有接近满幅才渐进压缩，
        # 且数学上永远 |out| < 0.98，不会溢出。
        peak = float(np.abs(dst).max())
        if peak > 0.7:
            self._last_pre_peak = max(self._last_pre_peak, peak)   # 限幅前的真实峰值（诊断）
            dst = 0.98 * np.tanh(dst / 0.98)
            self._soft_clips += 1
        # 防持续过载：软限幅只处理瞬时，若整段都压在近满幅（真实音乐常见），
        # 再叠一个慢速安全增益，把电平拉回可控范围，避免长时间听感发糊。
        rms = float(np.sqrt((dst ** 2).mean()))
        if rms > 0.35:
            self._safety_gain = max(0.25, self._safety_gain * 0.995)
        elif rms < 0.18:
            self._safety_gain = min(1.0, self._safety_gain * 1.002)
        if self._safety_gain < 0.999:
            dst = dst * self._safety_gain
        return (np.clip(dst, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
