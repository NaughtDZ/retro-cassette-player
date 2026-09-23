"""音频引擎：ffmpeg 子进程把任意格式解码为原始 PCM，经管道喂给音频输出设备。

- 解码走 ffmpeg 管道（任意格式），读取线程把 PCM 塞进有上限的缓冲（背压，不丢未播数据）。
- 输出走 Qt6.11 的 QAudioSink：``start()`` 返回可写 QIODevice，数据经它投递。
- 播放位置 = **已投递字节数**（设备写入量；无音频设备时按真实时间虚拟节流），
  因此 offscreen / 无音卡环境下位置依然按实时推进。
- 暂停/跳转通过重启 ffmpeg 进程并带 ``-ss`` 偏移实现，代际号 ``_gen`` 防旧线程污染状态。
"""
import os
import shutil
import subprocess
import threading
import time
from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal, QByteArray
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudio

from .proc_util import popen_hidden
from .tape_fx import TapeFx

SAMPLE_RATE = 48000
CHANNELS = 2
BITS = 16
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * (BITS // 8)   # 384000 B/s
CHUNK = 8192
BUFFER_CAP_SECONDS = 8          # 解码前瞻上限（超过则读取线程等待消费）
MAX_TICK_SECONDS = 0.25         # 单次 tick 最多折算的实时量，防止卡顿后一次性猛拉
LOOKAHEAD_SECONDS = 0.18        # 设备模式允许的额外前瞻，保证不欠载又不超前太多


def find_ffmpeg(explicit_dir=None):
    """定位 ffmpeg/ffprobe。优先项目 tools\\ffmpeg，其次系统 PATH。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = []
    if explicit_dir:
        candidates.append(explicit_dir)
    candidates.append(os.path.join(here, "tools", "ffmpeg"))
    for d in candidates:
        exe = os.path.join(d, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        probe = os.path.join(d, "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.isfile(exe) and os.path.isfile(probe):
            return {"dir": d, "ffmpeg": exe, "ffprobe": probe}
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            d = os.path.dirname(p)
            probe = os.path.join(d, "ffprobe" + (".exe" if os.name == "nt" else ""))
            if os.path.isfile(probe):
                return {"dir": d, "ffmpeg": p, "ffprobe": probe}
    raise RuntimeError("未找到可用的 ffmpeg/ffprobe：请把静态构建放进 tools\\ffmpeg")


class AudioEngine(QObject):
    position_changed = Signal(float)   # 当前播放位置（秒）
    track_ended = Signal()            # 曲目自然结束或解码失败
    playing_changed = Signal(bool)
    volume_changed = Signal(float)    # 音量（0..1 线性）

    def __init__(self, ffmpeg_dir: str, parent=None):
        super().__init__(parent)
        self.ffmpeg_exe = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        self._proc = None
        self._gen = 0                 # 进程代际号，防止旧读线程污染状态
        self._buf = deque()
        self._lock = threading.Lock()
        self._consumed = 0            # 已投递给输出的字节数（位置基准）
        self._decoded = 0             # 已从管道读出的字节数
        self._eof_pending = False
        self._failed = False
        self.path = None
        self.playing = False
        self.volume = 0.8             # 默认音量（面板旋钮控制，退出时随会话记住）
        # 磁带效果：DSP 在解码线程里处理 PCM（不占 GUI 线程）；DLL 缺失时自动直通
        self.tape_fx = TapeFx()
        self._last_feed_t = time.monotonic()
        self._anchor_t = self._last_feed_t     # 实时配额锚点（时间）
        self._anchor_bytes = 0                 # 实时配额锚点（字节）

        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        # Qt6.11 绑定：位宽经 setSampleFormat 指定（无 setSampleSize / setSampleType）
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        try:
            self._sink = QAudioSink(fmt, self)
        except Exception:
            self._sink = None         # 无音频设备（如 offscreen）时降级为虚拟实时播放
        self._device = None           # Qt6.11：start() 返回的 QIODevice，数据经它写入
        self._sink_failed = False
        self._starved = 0             # 设备连续拒收次数

        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(30)
        self._feed_timer.timeout.connect(self._feed)
        self._pos_timer = QTimer(self)
        self._pos_timer.setInterval(150)
        self._pos_timer.timeout.connect(lambda: self.position_changed.emit(self.position()))
        self._pos_timer.start()       # 常驻：位置每秒刷新约 7 次，计数器数字才会走动

    # ---------------- 解码子进程 ----------------
    def _spawn(self, path, offset):
        cmd = [self.ffmpeg_exe, "-hide_banner", "-loglevel", "error"]
        if offset > 0.05:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += ["-i", str(path), "-vn", "-acodec", "pcm_s16le",
                "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-f", "s16le", "pipe:1"]
        self.tape_fx.prepare(SAMPLE_RATE, CHUNK // (CHANNELS * (BITS // 8)))
        env = os.environ.copy()
        bindir = os.path.dirname(os.path.abspath(self.ffmpeg_exe))
        if bindir not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        with self._lock:
            self._buf.clear()
            self._consumed = int(offset * BYTES_PER_SEC)
            self._decoded = 0
        self._eof_pending = False
        self._failed = False
        self._last_feed_t = time.monotonic()
        # 实时配额锚点：位置最多超前 LOOKAHEAD_SECONDS，保证不因设备吞得快而"瞬播完"
        self._anchor_t = self._last_feed_t
        self._anchor_bytes = int(offset * BYTES_PER_SEC)
        try:
            self._proc = popen_hidden(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, env=env)
        except Exception as e:
            print(f"[audio] spawn 失败：{e}")
            return
        gen = self._gen
        threading.Thread(target=self._read_loop, args=(self._proc, gen), daemon=True).start()

    def _read_loop(self, proc, gen):
        """把解码数据搬进缓冲；缓冲满则等待消费（背压，宁可让 ffmpeg 等也不丢数据）。"""
        cap = BYTES_PER_SEC * BUFFER_CAP_SECONDS
        try:
            while True:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                if self.tape_fx.available:
                    data = self.tape_fx.process(data)      # 磁带染色（后台线程，不阻塞 GUI）
                with self._lock:
                    self._buf.append(data)
                    self._decoded += len(data)
                # 背压（注意：等待循环内不得再 append，否则同一块数据会被重复入队）
                while True:
                    with self._lock:
                        total = sum(len(b) for b in self._buf)
                    if total <= cap or gen != self._gen or not self.playing:
                        break
                    time.sleep(0.01)
        except Exception:
            pass
        code = proc.wait()
        if gen != self._gen:          # 已被 pause/seek/load 替换的旧线程
            return
        if code != 0:
            self._failed = True
        self._eof_pending = True

    def _close_proc(self):
        """掐掉解码进程。注意：不阻塞 GUI 线程 —— 不关管道、不等待进程退出。

        旧实现里 ``p.stdout.close()`` 会与读取线程争用同一句柄、``wait()`` 还要等进程
        真正结束，这些都发生在主线程且正好在换带动画起步的一瞬间，直接表现为"卡一下"。
        现在只发 kill，回收交给后台线程（daemon），读取线程会因管道断开自行退出。
        """
        self._gen += 1
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                threading.Thread(target=p.wait, daemon=True).start()
            except Exception:
                pass

    # ---------------- 音频输出（Qt6.11：start() → QIODevice 写入） ----------------
    def _restart_sink(self):
        """(重)启音频输出并取得可写 QIODevice；失败时降级为虚拟实时模式。"""
        self._device = None
        self._sink_failed = False
        if self._sink is None:
            return
        try:
            if self._sink.state() == QAudio.State.ActiveState and self._device is not None:
                return                      # 设备已在跑：沿用现有 QIODevice，避免重复 start 警告
            if self._sink.state() != QAudio.State.StoppedState:
                self._sink.stop()
            self._device = self._sink.start() or None
            self._apply_volume()            # 每次起设备都把面板音量重新灌进去
            if self._device is None:
                self._sink_failed = True
        except Exception as e:
            print(f"[audio] sink 启动失败：{e}")
            self._sink_failed = True

    def _apply_volume(self):
        try:
            if self._sink is not None:
                self._sink.setVolume(self.volume)     # Qt6 收 0..1 线性值，内部自行转对数
        except Exception:
            pass

    def set_volume(self, v):
        """设置音量（0..1 线性）；无音频设备的虚拟实时模式下只记录，不报错。"""
        v = max(0.0, min(1.0, float(v)))
        if abs(v - self.volume) < 1e-4:
            return
        self.volume = v
        self._apply_volume()
        self.volume_changed.emit(v)

    def _ensure_device(self):
        """确保输出设备可用（懒启动；设备故障后不再反复重试）。"""
        if self._device is not None or self._sink is None or self._sink_failed:
            return self._device
        try:
            if self._sink.error() != QAudio.Error.NoError:
                self._sink_failed = True
                return None
            if self._sink.state() in (QAudio.State.IdleState, QAudio.State.SuspendedState,
                                      QAudio.State.StoppedState):
                self._device = self._sink.start() or None
                self._apply_volume()
        except Exception as e:
            print(f"[audio] sink 启动失败：{e}")
            self._sink_failed = True
        return self._device

    def _feed(self):
        """按实时节奏把 PCM 投递给输出；顺带检查曲目结束（无音卡同样按实时推进）。"""
        now = time.monotonic()
        if not (self.playing and self.path):
            self._last_feed_t = now       # 暂停/停止期间不累积时间
            self._check_eof()
            return
        dt = min(MAX_TICK_SECONDS, max(0.0, now - self._last_feed_t))
        self._last_feed_t = now

        dev = self._device or self._ensure_device()
        # 实时配额：本 tick 最多投递到"已播时间 + 前瞻"对应的字节位置
        quota = (int((now - self._anchor_t) * BYTES_PER_SEC)
                 + int(LOOKAHEAD_SECONDS * BYTES_PER_SEC)
                 - (self._consumed - self._anchor_bytes))
        if quota <= 0:
            self._check_eof()
            return
        if dev is not None:
            # 提交量由设备可写空间决定；write() 的返回值在本平台不可靠，故不据其回填
            try:
                free = max(0, int(self._sink.bytesFree()))
            except Exception:
                free = BYTES_PER_SEC
            need = min(free, quota)
            if need <= 0:
                self._starved += 1
                if self._starved > 40:        # 约 1.2 秒设备完全不消费 → 降级虚拟实时
                    print("[audio] 输出设备不消费数据，降级为虚拟实时模式")
                    self._sink_failed = True
                    self._device = None
                    self._starved = 0
                self._check_eof()
                return
            self._starved = 0
        else:
            need = min(int(dt * BYTES_PER_SEC), quota)   # 虚拟实时：只消耗这段时间该播的量

        with self._lock:
            data = bytearray()
            while len(data) < need and self._buf:
                chunk = self._buf.popleft()
                remaining = need - len(data)     # 必须先算余量：len(data) 随后会被修改
                if len(chunk) <= remaining:
                    data += chunk
                else:
                    data += chunk[:remaining]
                    self._buf.appendleft(chunk[remaining:])
        if data:
            if dev is not None:
                try:
                    dev.write(QByteArray(bytes(data)))
                except Exception:
                    pass
            with self._lock:
                self._consumed += len(data)     # 以实际提交量计位置
        self._check_eof()

    def _check_eof(self):
        if not (self._eof_pending and self.path and self.playing):
            return
        with self._lock:
            drained = not self._buf
        if drained:
            self.playing = False
            self.playing_changed.emit(False)
            self.track_ended.emit()

    # ---------------- 对外 API ----------------
    def position(self) -> float:
        return self._consumed / BYTES_PER_SEC if self.path else 0.0

    # ---------------- 磁带效果（DSP 在 tape_fx 层，替换实现只改那一层） ----------------
    @property
    def tape_available(self):
        return self.tape_fx.available

    def set_tape_param(self, name, value):
        self.tape_fx.set(name, value)

    def set_tape_module(self, name, enabled):
        """分区开关：一键让某个效果区块失效（参数置中性，可恢复）。"""
        self.tape_fx.set_module(name, enabled)

    def tape_params(self):
        return dict(self.tape_fx.params)

    def export_tape_params(self):
        """用户视角的参数（含被分区开关记住的原值），用于写配置文件。"""
        return self.tape_fx.export_params()

    def tape_modules(self):
        return dict(self.tape_fx.modules)

    def tape_meters(self):
        """{'vu_l','vu_r','in_peak_l',...}：供主界面 VU 表读取。"""
        return self.tape_fx.meters()

    @property
    def failed(self):
        return self._failed

    def load(self, path, start_playing=True, offset=0.0):
        """加载新曲目（重启解码进程）。"""
        self._close_proc()
        self.path = str(path)
        self._restart_sink()
        self._spawn(self.path, offset)
        self.playing = bool(start_playing and self._proc is not None)
        self._feed_timer.start()
        self.position_changed.emit(offset)
        self.playing_changed.emit(self.playing)

    def pause(self):
        if not self.path or not self.playing:
            return
        pos = self.position()
        self._close_proc()
        with self._lock:
            self._buf.clear()
            self._consumed = int(pos * BYTES_PER_SEC)
        self.playing = False
        self.playing_changed.emit(False)

    def resume(self):
        if not self.path or self.playing:
            return
        self._spawn(self.path, self.position())
        self.playing = True
        self._feed_timer.start()
        self.playing_changed.emit(True)

    def seek(self, seconds):
        """跳转到指定秒；保持当前播放/暂停状态。"""
        if not self.path:
            return
        was_paused = not self.playing
        self._close_proc()
        pos = max(0.0, float(seconds))
        with self._lock:
            self._buf.clear()
            self._consumed = int(pos * BYTES_PER_SEC)
        if was_paused:
            return                      # 暂停态只记录位置，不重启解码
        self._spawn(self.path, pos)
        self.position_changed.emit(pos)

    def stop(self):
        """停止播放并归零。"""
        if not self.path:
            return
        self._close_proc()
        with self._lock:
            self._buf.clear()
            self._consumed = 0
        self.playing = False
        try:
            if self._sink is not None and self._sink.state() == QAudio.State.ActiveState:
                self._sink.stop()
            self._device = None
        except Exception:
            pass
        self.position_changed.emit(0.0)
        self.playing_changed.emit(False)
