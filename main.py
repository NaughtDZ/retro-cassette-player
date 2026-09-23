"""复古磁带播放器入口：装配音频引擎 / 播放列表模型 / 磁带机窗口 / 练习册悬浮窗。

运行：.venv\\Scripts\\python main.py   （或双击 run.bat）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QMessageBox

from app.audio_engine import AudioEngine, find_ffmpeg
from app.console_window import ConsoleWindow
from app.deck_widget import DeckWidget
from app import settings as settings_mod
from app.media_info import CoverWorker, ProbeWorker, scan_folder
from app.playlist_io import (PLAYLIST_FILTER, load_playlist, load_session, read_session_tracks,
                             save_m3u, save_session, split_existing)
from app.playlist_model import PlaylistModel
from app.playlist_window import PlaylistWindow
from app.shadow_window import ShadowWindow
from app.skin_loader import Skin

AUDIO_FILTER = ("音频文件 (*.mp3 *.flac *.m4a .aac *.ogg *.opus *.wav *.wma);;所有文件 (*)")


class PlayerApp:
    def __init__(self):
        self.root = os.path.dirname(os.path.abspath(__file__))
        ff = find_ffmpeg(os.path.join(self.root, "tools", "ffmpeg"))
        self.ffprobe_exe = ff["ffprobe"]
        self.ffmpeg_exe = ff["ffmpeg"]
        self.cache_dir = os.path.join(self.root, ".cache", "covers")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.session_path = os.path.join(self.root, ".cache", "session.json")
        # 软件配置：<项目根>\config\settings.json（不写用户目录/系统盘）
        self._had_settings = os.path.isfile(settings_mod.config_path(self.root))
        self.settings = settings_mod.load(self.root)

        self.engine = AudioEngine(ff["dir"])
        self.model = PlaylistModel()
        self.skin_name = str(self.settings.get("skin") or "default")
        self.want_play = True
        self._cover_workers: list[CoverWorker] = []
        self._probe_worker = None

        deck_content = DeckWidget(self._make_skin())
        self.deck_win = ShadowWindow(margin=18, blur=30)
        self.deck_win.add_content(deck_content)
        self.deck = deck_content
        self.pl_win = PlaylistWindow()
        # 磁带 console 悬浮窗：按底座按钮呼出（默认隐藏）
        self.console_win = ConsoleWindow(self.deck.skin, self.engine.tape_params())
        self.console_win.param_changed.connect(self.on_console_param)
        self.console_win.module_toggled.connect(self.engine.set_tape_module)
        self.console_win.power_toggled.connect(
            lambda on: self.engine.set_tape_param("active", 1.0 if on else 0.0))

        # ---------------- 信号装配 ----------------
        self.deck.action.connect(self.on_action)
        self.deck.menu_requested.connect(lambda pos: self.show_menu_at(pos.toPoint()))
        self.engine.position_changed.connect(self.on_position)
        self.engine.track_ended.connect(self.on_track_end)
        self.engine.playing_changed.connect(self.deck.set_playing)   # 播放键状态只认引擎，切歌/换带后不会错位
        self.deck.seek_requested.connect(self.on_seek_requested)
        self.deck.volume_changed.connect(self.engine.set_volume)   # 面板旋钮 → 输出设备音量

        # ---------------- VU 表：按 ~90ms 读 DSP 表头读数 ----------------
        if self.engine.tape_available:
            print(f"[fx] 磁带效果已就绪（延迟 {self.engine.tape_fx.latency_samples()} samples）")
        else:
            print(f"[fx] 磁带效果未启用：{self.engine.tape_fx.reason}")
        self._meter_timer = QTimer(self.deck_win)
        self._meter_timer.setInterval(90)
        self._meter_timer.timeout.connect(self._poll_meters)
        self._meter_timer.start()
        self.model.changed.connect(lambda: self.pl_win.rebuild(self.model.tracks, self.model.current))
        self.model.current_changed.connect(self.pl_win.set_current)   # 切歌只重绘列表两行
        self.pl_win.play_requested.connect(self._play_index)
        self.deck._anim.swap_requested.connect(self._on_swap_fired)

        # ---------------- 初始布局：磁带机居中，练习册在右侧 ----------------
        scr = QApplication.primaryScreen().availableGeometry()
        dw, dh = 960 + 24, 620 + 24
        x = max(0, (scr.width() - dw) // 2)
        y = max(0, (scr.height() - dh) // 2 - 8)
        self.deck_win.move(x, y)
        self.pl_win.move(min(x + dw + 36, scr.right() - 500), y + 48)
        self.deck_win.show()
        self.pl_win.show()

        # ---------------- 恢复配置与上次会话；退出时自动记住 ----------------
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_settings)
            app.aboutToQuit.connect(self._save_session)
        self._apply_settings()
        self._restore_session()

    # ---------------- 软件配置（<项目根>\config\settings.json） ----------------
    def _apply_settings(self):
        """应用配置里的音量、效果器参数/开关、窗口位置。"""
        s = self.settings
        try:
            self.deck.set_volume(float(s.get("volume", 0.8)))
        except (TypeError, ValueError):
            pass
        fx = s.get("fx") or {}
        for k, v in (fx.get("params") or {}).items():
            try:
                self.engine.set_tape_param(k, float(v))
            except (TypeError, ValueError):
                continue
        for name, on in (fx.get("modules") or {}).items():
            self.engine.set_tape_module(name, bool(on))
        self.engine.set_tape_param("active", 0.0 if fx.get("bypass") else 1.0)

        w = s.get("window") or {}
        pos = settings_mod.window_pos(s, "deck")
        if pos is not None:
            self.deck_win.move(*pos)
        if w.get("playlist_topmost"):
            self.pl_win.setWindowFlags(self.pl_win.windowFlags() | Qt.WindowStaysOnTopHint)
            self.pl_win.show() if w.get("playlist_visible") else self.pl_win.hide()

    def _save_settings(self):
        """退出时把音量、皮肤、效果器参数与开关、窗口位置写进配置文件。"""
        s = dict(self.settings)
        s["version"] = 1
        s["volume"] = round(float(self.deck.volume), 3)
        s["skin"] = self.skin_name
        s["fx"] = {
            "bypass": float(self.engine.tape_params().get("active", 1.0)) <= 0.5,
            "params": {k: round(float(v), 4) for k, v in self.engine.export_tape_params().items()},
            "modules": self.engine.tape_modules(),
        }
        s["window"] = {
            "deck": [self.deck_win.x(), self.deck_win.y()],
            "console": [self.console_win.x(), self.console_win.y()],
            "console_visible": bool(self.console_win.isVisible()),
            "playlist_topmost": bool(self.pl_win.windowFlags() & Qt.WindowStaysOnTopHint),
            "playlist_visible": bool(self.pl_win.isVisible()),
        }
        if settings_mod.save(self.root, s):
            print(f"[config] 已保存到 {settings_mod.config_path(self.root)}")

    # ---------------- 播放列表持久化 ----------------
    def _save_session(self):
        """退出时记住播放会话（列表 / 当前曲目 / 模式 / 进度）。

        音量与效果器参数不在这里——它们属于软件配置，写在 config\\settings.json。
        """
        try:
            pos = self.engine.position() if self.engine.path else 0.0
            save_session(self.session_path, self.model.tracks, self.model.current,
                         self.model.loop_mode, pos)
        except Exception as e:
            print(f"[session] 保存失败：{e}")

    def _restore_session(self):
        """启动时恢复上次的列表与模式（不自动出声，只把磁带上带画面准备好）。

        元数据优先用会话快照回填，因此上千首的列表也能秒开；只有快照里缺元数据的
        前若干首会补探测，其余等真正播放到它时再读，不会一启动就后台扫盘。
        """
        data = load_session(self.session_path)
        if not data:
            return
        if not self._had_settings:
            # 首次运行（还没有配置文件）：把旧会话里的音量与效果器参数继承过来，之后各归各处
            vol = data.get("volume")
            if vol is not None:
                try:
                    self.deck.set_volume(float(vol))
                except (TypeError, ValueError):
                    pass
            tape = data.get("tape")
            if isinstance(tape, dict):
                for k, v in tape.items():
                    try:
                        self.engine.set_tape_param(k, float(v))
                    except (TypeError, ValueError):
                        continue
        entries = read_session_tracks(data)
        ok, missing = split_existing([e["path"] for e in entries])
        if missing:
            print(f"[session] 跳过 {len(missing)} 个已不存在或已移动的文件")
        keep = {os.path.abspath(p) for p in ok}
        entries = [e for e in entries if os.path.abspath(e["path"]) in keep]
        if not entries:
            return
        self.model.load_files([e["path"] for e in entries])
        self._apply_session_meta(entries)
        mode = int(data.get("loop_mode") or 0)
        self.model.set_loop_mode(mode)
        self.deck.set_loop_mode(mode)
        # 注意不能用 `data.get("current") or -1`：索引 0 是合法值但 falsy，
        # 会被 `or` 吃掉，导致"上次停在第 1 首"时整个恢复分支被跳过。
        cur_raw = data.get("current")
        cur = int(cur_raw) if cur_raw is not None else -1
        if 0 <= cur < len(self.model.tracks):
            self.model.set_current(cur)
            t = self.model.tracks[cur]
            sub = " · ".join(x for x in (t.artist, t.album) if x)
            self.deck.set_labels(t.title, sub)
            position = float(data.get("position") or 0.0)
            self.deck.set_progress(position, t.duration)
            self._cover_for(t.path)
            self.want_play = False
            self.deck.set_playing(False)
            self._preload(t, position)
        print(f"[session] 已恢复播放列表：{len(entries)} 首，模式 {mode}"
              f"（当前第 {cur + 1} 首，进度 {float(data.get('position') or 0.0):.1f}s）")

    def _preload(self, track, position):
        """把上次的曲目与进度装进引擎但不发声：按下播放键即可从原处继续。

        只解码不投递（``start_playing=False``），所以启动时不会出声；
        顺带把首段 PCM 备好，按播放时起声更快。
        """
        try:
            self.engine.load(track.path, start_playing=False, offset=max(0.0, float(position)))
            self.deck.set_progress(max(0.0, float(position)), track.duration)
        except Exception as e:
            print(f"[session] 预载 {track.path} 失败：{e}")

    SESSION_PROBE_LIMIT = 60

    def _apply_session_meta(self, entries):
        """把会话快照写回模型；快照缺失的曲目只补探测前若干首。"""
        need = []
        for e in entries:
            if e.get("duration"):
                self.model.apply_meta(e["path"], {
                    "title": e.get("title") or None,
                    "artist": e.get("artist", ""),
                    "album": e.get("album", ""),
                    "duration": float(e["duration"]),
                })
            else:
                need.append(e["path"])
        if not need:
            print(f"[session] 元数据全部来自快照，无需探测（{len(entries)} 首）")
            return
        self._probe(need[:self.SESSION_PROBE_LIMIT])
        rest = len(need) - self.SESSION_PROBE_LIMIT
        if rest > 0:
            print(f"[session] 另有 {rest} 首元数据留到播放时再读取")

    def open_playlist_file(self):
        """从 m3u8 / m3u / json 载入播放列表。"""
        f, _sel = QFileDialog.getOpenFileName(self.deck_win, "打开播放列表", "", PLAYLIST_FILTER)
        if not f:
            return
        try:
            paths = load_playlist(f)
        except Exception as e:
            QMessageBox.warning(self.deck_win, "打开播放列表", f"读取失败：{e}")
            return
        ok, missing = split_existing(paths)
        if missing:
            print(f"[playlist] 跳过 {len(missing)} 个不存在的文件")
        if not ok:
            QMessageBox.information(self.deck_win, "打开播放列表", "列表里没有可用的音频文件。")
            return
        self.want_play = True
        self.load_files(ok)

    def save_playlist_file(self):
        """把当前列表存成 M3U8（UTF-8，别的播放器也能读）。"""
        if not self.model.tracks:
            QMessageBox.information(self.deck_win, "保存播放列表", "当前播放列表是空的。")
            return
        f, _sel = QFileDialog.getSaveFileName(self.deck_win, "保存播放列表",
                                              "播放列表.m3u8", PLAYLIST_FILTER)
        if not f:
            return
        if not os.path.splitext(f)[1]:
            f += ".m3u8"
        try:
            save_m3u(f, self.model.tracks)
        except Exception as e:
            QMessageBox.warning(self.deck_win, "保存播放列表", f"写入失败：{e}")
            return
        print(f"[playlist] 已保存 {len(self.model.tracks)} 首 → {f}")

    # ---------------- 皮肤 ----------------
    def _make_skin(self):
        d = os.path.join(self.root, "skins", self.skin_name)
        return Skin(self.skin_name, d if os.path.isdir(d) else None)

    def apply_skin(self, name):
        self.skin_name = name
        self.deck.set_skin(Skin(name, os.path.join(self.root, "skins", name)))
        self.console_win.set_skin(self.deck.skin)          # console 面板跟着换皮

    # ---------------- 磁带 console ----------------
    def on_console_param(self, name, value):
        self.engine.set_tape_param(name, value)

    def toggle_console(self):
        """在磁带机旁显示/隐藏 console 面板（屏幕放不下就翻到另一侧）。"""
        if self.console_win.isVisible():
            self.console_win.hide()
            return
        tp = self.engine.tape_params()
        self.console_win.sync_state(tp, self.engine.tape_modules(),
                                    float(tp.get("active", 1.0)) > 0.5)
        self.console_win.adjustSize()
        saved = settings_mod.window_pos(self.settings, "console")
        if saved is not None:
            self.console_win.move(*saved)
            self.console_win.show()
            self.console_win.raise_()
            return
        scr = QApplication.primaryScreen().availableGeometry()
        dg = self.deck_win.geometry()
        w = self.console_win.width()
        x = dg.right() + 24
        if x + w > scr.right():
            x = dg.left() - w - 24
        x = max(scr.left() + 8, x)
        y = max(scr.top() + 8, min(dg.top(), scr.bottom() - self.console_win.height() - 8))
        self.console_win.move(x, y)
        self.console_win.show()
        self.console_win.raise_()

    # ---------------- 播放列表装载 ----------------
    def _refresh_list(self):
        self.pl_win.rebuild(self.model.tracks, self.model.current)

    def load_files(self, paths):
        if not paths:
            return
        self.model.load_files(paths)
        self._probe([t.path for t in self.model.tracks])
        if self.model.current < 0 and self.model.tracks:
            self.want_play = True
            self._play_index(0)

    def add_files(self, paths):
        before = len(self.model.tracks)
        self.model.add_files(paths)
        if len(self.model.tracks) > before:
            self._probe([t.path for t in self.model.tracks[before:]])

    def _probe(self, paths):
        w = ProbeWorker(self.ffprobe_exe, paths)
        w.one.connect(lambda p, meta: self.model.apply_meta(p, meta))
        w.start()
        self._probe_worker = w

    # ---------------- 封面 ----------------
    def _cover_for(self, path):
        for w in self._cover_workers:
            if w.path == str(path) and w.isRunning():
                return
        w = CoverWorker(self.ffmpeg_exe, self.cache_dir, path)
        w.ready.connect(self._on_cover_ready)
        w.start()
        self._cover_workers.append(w)

    def _on_cover_ready(self, path, pm):
        cur = self.model.current
        if 0 <= cur < len(self.model.tracks) and self.model.tracks[cur].path == str(path):
            self.deck.set_cover(pm)

    # ---------------- 播放控制 ----------------
    def _play_index(self, idx, burst=0):
        n = len(self.model.tracks)
        if not (0 <= idx < n):
            return
        eject = self.model.needs_eject(self.model.current, idx)
        self.model.set_current(idx)
        t = self.model.tracks[idx]
        if not t.duration:                     # 会话快照没覆盖到的曲目：播放时才补读元数据
            self._probe([t.path])
        sub = " · ".join(x for x in (t.artist, t.album) if x)
        self.deck.set_labels(t.title, sub)
        self._cover_for(t.path)
        for j in (idx - 1, idx + 1):          # 预热相邻曲目封面缓存
            if 0 <= j < n and j != idx:
                self._cover_for(self.model.tracks[j].path)
        if eject:
            if self.engine.playing:
                self.engine.pause()           # 旧带拔出时停播
            self.deck.trigger_swap()          # 换带动画；_on_swap_fired 时起播新带
        else:
            self.engine.load(t.path, start_playing=self.want_play)
            if burst:
                self.deck.seek_burst(burst)

    def _on_swap_fired(self):
        cur = self.model.current
        if 0 <= cur < len(self.model.tracks):
            t = self.model.tracks[cur]
            self.engine.load(t.path, start_playing=self.want_play)
            self.deck.set_progress(0.0, t.duration)

    def on_position(self, pos):
        cur = self.model.current
        if 0 <= cur < len(self.model.tracks):
            self.deck.set_progress(pos, self.model.tracks[cur].duration)

    def _poll_meters(self):
        """把 DSP 表头读数喂给主界面 VU 表；没在播放就送零值让指针回落。"""
        if self.engine.playing:
            self.deck.set_meters(self.engine.tape_meters())
        else:
            self.deck.set_meters({})

    def on_seek_requested(self, seconds):
        """观察窗进度条拖动结束：跳转到该位置（暂停态只移动位置，不自动起播）。"""
        if self.engine.path is None:
            return
        cur = self.model.current
        dur = self.model.tracks[cur].duration if 0 <= cur < len(self.model.tracks) else 0.0
        old = self.engine.position()
        t = max(0.0, min(float(seconds), dur - 0.15)) if dur > 0.3 else max(0.0, float(seconds))
        self.engine.seek(t)
        self.deck.set_progress(t, dur)
        self.deck.seek_burst(1 if t >= old else -1, 2.6)

    def on_track_end(self):
        failed = self.engine.failed
        if failed:
            print(f"[player] 解码失败，跳过：{self.engine.path}")
        j = self.model.auto_next_index()
        n = len(self.model.tracks)
        if failed and n > 1 and (j < 0 or j == self.model.current):
            j = (max(0, self.model.current) + 1) % n      # 跳过坏文件继续播
        if j < 0:
            self.want_play = False
            self.deck.set_playing(False)
            return
        self.want_play = True
        self._play_index(j)

    def toggle_play(self):
        if not self.model.tracks:
            return
        if self.engine.path is None or self.model.current < 0:
            # 没有预载过的曲目：优先从上次记住的那首开始，而不是永远第 0 首
            self.want_play = True
            idx = self.model.current if 0 <= self.model.current < len(self.model.tracks) else 0
            self._play_index(idx)
            return
        if self.engine.playing:
            self.engine.pause()
            self.want_play = False
            self.deck.set_playing(False)
        else:
            self.engine.resume()
            self.want_play = True
            self.deck.set_playing(True)

    def on_action(self, a):
        n = len(self.model.tracks)
        if a == "toggle_play":
            self.toggle_play()
        elif a == "next" and n:
            self._play_index(self.model.manual_next(), burst=+1)
        elif a == "prev" and n:
            self._play_index(self.model.manual_prev(), burst=-1)
        elif a == "stop":
            if self.engine.path is not None:
                cur = self.model.current
                dur = self.model.tracks[cur].duration if 0 <= cur < n else 0.0
                self.engine.stop()
                self.deck.set_progress(0.0, dur)
            self.want_play = False
            self.deck.set_playing(False)
        elif a == "loop":
            m = (self.model.loop_mode + 1) % 4        # 顺序 → 单曲 → 列表 → 随机
            self.model.set_loop_mode(m)
            self.deck.set_loop_mode(m)
        elif a == "min":
            self.deck_win.showMinimized()
        elif a == "list":
            if self.pl_win.isVisible():
                self.pl_win.hide()
            else:
                self.pl_win.show()
                self.pl_win.raise_()
        elif a == "console":
            self.toggle_console()
        elif a == "close":
            QApplication.instance().quit()
        elif a == "seek_fwd" and self.engine.path is not None:
            cur = self.model.current
            dur = self.model.tracks[cur].duration if 0 <= cur < n else 0.0
            target = min(self.engine.position() + 5, max(0.0, dur - 0.2)) if dur > 0 else self.engine.position() + 5
            self.engine.seek(target)
            self.deck.seek_burst(+1, 2.2)
        elif a == "seek_back" and self.engine.path is not None:
            self.engine.seek(max(0.0, self.engine.position() - 5))
            self.deck.seek_burst(-1, 2.2)

    # ---------------- 菜单 ----------------
    def show_menu_at(self, pos):
        menu = QMenu()
        act_open_f = menu.addAction("打开文件…")
        act_open_d = menu.addAction("打开文件夹…")
        act_add = menu.addAction("添加到播放列表…")
        menu.addSeparator()
        act_pl_open = menu.addAction("打开播放列表…")
        act_pl_save = menu.addAction("保存播放列表…")
        menu.addSeparator()

        skins_dir = os.path.join(self.root, "skins")
        sm = QMenu("皮肤", menu)
        for name in Skin.list_packs(skins_dir):
            a = sm.addAction(name)
            a.setCheckable(True)
            a.setChecked(name == self.skin_name)
            a.triggered.connect(lambda _c=False, n=name: self.apply_skin(n))
        menu.addMenu(sm)

        topmost = bool(self.pl_win.windowFlags() & Qt.WindowStaysOnTopHint)
        act_top = menu.addAction("播放列表窗口置顶")
        act_top.setCheckable(True)
        act_top.setChecked(topmost)
        act_about = menu.addAction("关于")

        def do_open_files():
            files, _f = QFileDialog.getOpenFileNames(self.deck_win, "打开音频文件", "", AUDIO_FILTER)
            if files:
                self.load_files(files)

        def do_add_files():
            files, _f = QFileDialog.getOpenFileNames(self.deck_win, "添加音频文件", "", AUDIO_FILTER)
            if files:
                self.add_files(files)

        act_open_f.triggered.connect(do_open_files)
        act_open_d.triggered.connect(
            lambda: (lambda d: self.load_files(scan_folder(d)))(QFileDialog.getExistingDirectory(self.deck_win, "打开文件夹")))
        act_add.triggered.connect(do_add_files)
        act_pl_open.triggered.connect(self.open_playlist_file)
        act_pl_save.triggered.connect(self.save_playlist_file)
        act_top.triggered.connect(lambda: self._toggle_pl_topmost())
        act_about.triggered.connect(
            lambda: QMessageBox.about(self.deck_win, "复古磁带播放器",
                                      "复古磁带播放器 v1.0\n底层：ffmpeg 解码 · PySide6 矢量界面\n皮肤包目录：skins\\"))

        menu.exec(pos)

    def _toggle_pl_topmost(self):
        flags = self.pl_win.windowFlags()
        if flags & Qt.WindowStaysOnTopHint:
            flags &= ~Qt.WindowStaysOnTopHint
        else:
            flags |= Qt.WindowStaysOnTopHint
        self.pl_win.setWindowFlags(flags)
        self.pl_win.show()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Retro Cassette Player")
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    if os.path.isfile(ico):
        app.setWindowIcon(QIcon(ico))         # 任务栏 / 快捷方式图标
    PlayerApp()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
