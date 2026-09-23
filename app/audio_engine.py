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

from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QAudio

from .proc_util import popen_hidden
from .tape_fx import TapeFx

SAMPLE_RATE = 48000
CHANNELS = 2
BITS = 16
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * (BITS // 8)   # 192000 B/s
CHUNK = 8192
BUFFER_CAP_SECONDS = 3          # 解码前瞻上限（超过则读取线程等待消费）
MAX_TICK_SECONDS = 0.25         # 单次 tick 最多折算的实时量，防止卡顿后一次性猛拉
LOOKAHEAD_SECONDS = 0.12        # 虚拟实时模式下的额外前瞻
OUTPUT_BUFFER_SECONDS = 0.12    # 真实输出设备缓冲：低到不影响手感，又留出抗抖动余量
SIGNAL_PATH_THRU = 3.0          # signal_path 的 Thru 档：core 在此档直接 return input

# ★ PySide6 的 QAudio.State 枚举实例之间用 == / is 比较**不可靠**：同一个状态取两次会得到
# 不同实例、判为不相等（实测 kind="StoppedState"、value 都是 2，`==` 却返回 False）。
# 一旦用它判断，逻辑会整体走反（设备永远不被 start → 播放彻底没声音）。全部改比 value。
_AUDIO_ACTIVE = int(QAudio.State.ActiveState.value)
_AUDIO_IDLE = int(QAudio.State.IdleState.value)
_AUDIO_STOPPED = int(QAudio.State.StoppedState.value)
_AUDIO_SUSPENDED = int(QAudio.State.SuspendedState.value)


def _sink_state_code(sink) -> int:
    """QAudioSink 当前状态的状态码；取不到返回 -1。比较一律用这个，不要用枚举相等。"""
    try:
        st = sink.state()
    except Exception:
        return -1
    return int(getattr(st, "value", st))


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


class PcmSource(QIODevice):
    """QAudioSink 的数据源（pull 模式：设备主动来拉，而不是我们往里推）。

    拉数据的时机由 Qt 的音频线程决定，磁带 DSP 就在拉取现场处理，好处是两头都躲开了：

    - **不在 GUI 线程**：打开 console 面板、换带动画这类界面卡顿再也不会把它饿死。
      旧实现把 DSP 与投递都放在 GUI 线程，界面一卡输出缓冲就被抽空，听感就是爆音/白噪音。
    - **不在解码线程**：不经过秒级的解码预读，改参数最多一个设备缓冲就生效。

    代价是 readData 运行在音频线程上，所以它只做"取缓冲 + 过 DSP"，绝不阻塞、绝不做 UI 操作。
    """

    def __init__(self, engine):
        super().__init__()
        self._eng = engine
        self.reads = 0                      # 诊断用：被设备拉取的次数
        # ★ pull 模式的前提：QAudioSink.start(device) 要求源设备已经以只读方式打开。
        # 忘了这一句，设备就永远不来读——表现是"播放进度在走、但一点声音都没有"。
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self):
        return True

    def bytesAvailable(self):
        with self._eng._lock:
            n = sum(len(b) for b in self._eng._buf)
        return n + super().bytesAvailable()

    def readData(self, maxlen):
        eng = self._eng
        need = int(maxlen)
        if need <= 0:
            return bytes(0)
        self.reads += 1
        with eng._lock:
            out = bytearray()
            while len(out) < need and eng._buf:
                chunk = eng._buf[0]
                room = need - len(out)
                if len(chunk) <= room:
                    out += chunk
                    eng._buf.popleft()
                else:
                    out += chunk[:room]
                    eng._buf[0] = chunk[room:]
            if out:
                eng._consumed += len(out)          # 位置以真正交给设备的字节为准
        if not out:
            return bytes(0)
        return bytes(eng._apply_tape_fx(out))

    def readLineData(self, maxlen):
        return bytes(0)

    def writeData(self, data):
        return 0


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
        # 调试静音：设了 RETRO_MUTE=1 就完全不打开输出设备（走虚拟实时，绝不出声）
        silent = bool(os.environ.get("RETRO_MUTE"))
        if silent:
            self._sink = None
            print("[audio] RETRO_MUTE 已开启：不打开音频输出设备（静音调试）")
        else:
            try:
                self._sink = QAudioSink(fmt, self)
            except Exception:
                self._sink = None         # 无音频设备（如 offscreen）时降级为虚拟实时播放
        self._device = None           # 兼容保留：pull 模式下由 _pull 表示设备在跑
        self._source = None           # PcmSource（pull 模式的取数源）
        self._pull = False            # True = 真实设备在主动拉数据
        self._drained = False         # pull 模式：解码结束且缓冲已抽干
        self._sink_failed = False
        self._starved = 0             # 设备连续拒收次数
        self._saved_signal_path = 0.0  # 总开关关掉前的 signal_path（打开时恢复）

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
        self._drained = False
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

    # ---------------- 音频输出（pull 模式：Qt 音频线程主动来拉数据） ----------------
    def _restart_sink(self):
        """(重)启音频输出，让设备从我们的 PcmSource 拉数据。

        ★ 只在设备**确实处于 Stopped** 时才 start。旧写法"状态不是 Active 就先 stop 再 start"
        会因为状态切换本身需要时间而每次 tick 都重启一次，抖动成死循环：设备反复 start，
        processedUSecs 每次归零，听感就是完全没声音（实测状态切换 199 次、实际播放 0.06 秒）。
        Active / Idle / Suspended 都表示设备在工作，一概不要去折腾它。
        """
        self._device = None
        if self._sink is None:
            return
        try:
            if _sink_state_code(self._sink) != _AUDIO_STOPPED:
                self._pull = True            # 已在工作：沿用现有设备，别 stop/start
                return
            if self._source is None:
                self._source = PcmSource(self)
            try:
                self._sink.setBufferSize(int(BYTES_PER_SEC * OUTPUT_BUFFER_SECONDS))
            except Exception:
                pass
            self._sink.start(self._source)
            self._pull = True
            self._apply_volume()            # 每次起设备都把面板音量重新灌进去
        except Exception as e:
            print(f"[audio] sink 启动失败：{e}")
            self._sink_failed = True
            self._pull = False

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
        """返回 True 表示设备正在拉数据（此时 _feed 绝不能再自己消耗缓冲）。

        两个坑都在这里：
        - ★ 缓冲为空时**不能** start：pull 模式下 Qt 第一次查询拿不到数据就停止拉取，
          结果是"进度在走、一点声音都没有"（实测 readData 调用 0 次、设备播放 0.00 秒）。
          所以必须有数据才启动设备。
        - 判据不用 sink.error()：偶发一次 UnderrunError 不代表设备坏了，用 error() 判死
          会让 _feed 误以为"没有设备"，转而自己消耗缓冲，把音频提前吃光（断音）。
        """
        if self._sink is None or self._sink_failed:
            return False
        code = _sink_state_code(self._sink)
        if code < 0:
            return False
        if code == _AUDIO_ACTIVE:
            self._pull = True
            return True
        if code == _AUDIO_IDLE and self._source is not None and self._source.reads > 0:
            self._pull = True              # 只是暂时供不上数据，设备本身在工作
            return True
        with self._lock:
            has_data = bool(self._buf)
        if not has_data:
            return False
        try:
            self._restart_sink()
        except Exception as e:
            print(f"[audio] sink 启动失败：{e}")
            self._sink_failed = True
            self._pull = False
        return self._pull

    def _feed(self):
        """维护播放推进与结束判定。

        pull 模式下数据由 Qt 的音频线程来拉，这里只管状态；没有真实设备时
        （offscreen / 无音卡）退回"虚拟实时"：按时间自己消耗缓冲，进度照常推进。
        """
        now = time.monotonic()
        if not (self.playing and self.path):
            self._last_feed_t = now       # 暂停/停止期间不累积时间
            self._check_eof()
            return
        dt = min(MAX_TICK_SECONDS, max(0.0, now - self._last_feed_t))
        self._last_feed_t = now

        if self._ensure_device():
            self._check_eof()             # 真实设备：投递已交给 Qt 的音频线程
            return

        # 虚拟实时：按时间配额消耗
        quota = (int((now - self._anchor_t) * BYTES_PER_SEC)
                 + int(LOOKAHEAD_SECONDS * BYTES_PER_SEC)
                 - (self._consumed - self._anchor_bytes))
        if quota <= 0:
            self._check_eof()
            return
        need = min(int(dt * BYTES_PER_SEC), quota)
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
                self._consumed += len(data)
        if data:
            self._apply_tape_fx(data)     # 无设备时不出声，但 DSP 链路照跑（VU / 参数一致）
        self._check_eof()

    def _apply_tape_fx(self, data: bytearray) -> bytearray:
        """在投递前过一遍磁带 DSP（参数改动最多一个投递周期就听得到）。

        只把"完整帧"交给 DSP：管道/缓冲切分不保证 4 字节整数倍，直接按 len//4
        处理会把声道错位、污染后面整条流（听感就是刺耳的沙沙声）。
        """
        if not self.tape_fx.available:
            return data
        raw = bytes(data)
        aligned = len(raw) - (len(raw) % 4)
        if not aligned:
            return data
        return bytearray(self.tape_fx.process(raw[:aligned]) + raw[aligned:])

    def _check_eof(self):
        if not (self.path and self.playing):
            return
        with self._lock:
            drained = not self._buf
        if drained and self._eof_pending:
            if self._pull and (self._source is not None
                               and self._source.bytesAvailable() > 0):
                return                          # 设备还没把缓冲抽完
            self._drained = False
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

    def set_tape_power(self, on: bool):
        """磁带机总开关。

        关掉时把 ``signal_path`` 切到 **Thru** —— core 在该档位直接 ``return input``，
        是真正零延迟、零染色的旁通（比只把 active 置 0 更彻底）；打开时恢复用户原来的档位。
        """
        try:
            if on:
                self.tape_fx.set("signal_path", self._saved_signal_path)
            else:
                self._saved_signal_path = float(self.tape_fx.params.get("signal_path", 0.0))
                self.tape_fx.set("signal_path", SIGNAL_PATH_THRU)
            self.tape_fx.set("active", 1.0 if on else 0.0)
        except Exception as e:
            print(f"[audio] 总开关切换失败：{e}")

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
        """加载新曲目（重启解码进程）。

        设备**不在这里启动**：此时缓冲必然是空的，pull 模式下 Qt 拿不到数据就会停止拉取。
        交给 ``_feed`` 等缓冲有数据后再启动。
        """
        self._close_proc()
        self.path = str(path)
        self._spawn(self.path, offset)
        self.playing = bool(start_playing and self._proc is not None)
        self._feed_timer.start()
        self._feed()
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
            if self._sink is not None and _sink_state_code(self._sink) != _AUDIO_STOPPED:
                self._sink.stop()
            self._device = None
        except Exception:
            pass
        self.position_changed.emit(0.0)
        self.playing_changed.emit(False)
