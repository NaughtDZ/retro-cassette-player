"""播放列表模型：曲目队列 + 播放模式（顺序/单曲/列表/随机） + 换带判定。"""
import os
import random

from PySide6.QtCore import QObject, Signal


class Track:
    __slots__ = ("path", "title", "artist", "album", "duration")

    def __init__(self, path):
        self.path = str(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        import re
        self.title = re.sub(r"^\s*\d{1,3}\s*[-._]\s*", "", stem).strip() or stem
        self.artist = ""
        self.album = ""
        self.duration = 0.0


class PlaylistModel(QObject):
    changed = Signal()            # 曲目内容（集合 / 元数据）变化
    current_changed = Signal(int)  # 仅当前曲目变化：列表只需重绘两行，不必整体重排

    MODE_SEQUENCE = 0            # 顺序播放（播完停止）
    MODE_REPEAT_ONE = 1          # 单曲循环
    MODE_REPEAT_ALL = 2          # 列表循环
    MODE_SHUFFLE = 3             # 随机播放

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks: list[Track] = []
        self.current = -1
        self.loop_mode = self.MODE_SEQUENCE
        self._shuffle_hist: list[int] = []    # 随机模式下的播放历史（供"上一首"回退）

    # ---------------- 装载 ----------------
    def load_files(self, paths):
        seen, tracks = set(), []
        for p in paths:
            p = str(p)
            if p not in seen:
                seen.add(p)
                tracks.append(Track(p))
        self.tracks = tracks
        self.current = -1
        self.changed.emit()

    def add_files(self, paths):
        existing = {t.path for t in self.tracks}
        added = False
        for p in paths:
            p = str(p)
            if p not in existing:
                existing.add(p)
                self.tracks.append(Track(p))
                added = True
        if added:
            self.changed.emit()

    def apply_meta(self, path, meta):
        for t in self.tracks:
            if t.path == str(path):
                t.title = meta.get("title") or t.title
                t.artist = meta.get("artist", "")
                t.album = meta.get("album", "")
                t.duration = float(meta.get("duration", 0.0) or 0.0)
                break
        self.changed.emit()

    # ---------------- 导航 ----------------
    def set_current(self, idx):
        if 0 <= idx < len(self.tracks) and idx != self.current:
            self.current = idx
            self.current_changed.emit(idx)

    def set_loop_mode(self, mode):
        """0=顺序播放 1=单曲循环 2=列表循环 3=随机播放。"""
        m = max(0, min(3, int(mode)))
        if m != self.loop_mode:
            self.loop_mode = m
            self._shuffle_hist.clear()
            self.changed.emit()

    def _random_index(self):
        """随机取一个与当前不同的索引（列表只有一首时返回 0）。"""
        n = len(self.tracks)
        if n <= 1:
            return 0 if n else -1
        i = self.current
        j = i
        while j == i:
            j = random.randrange(n)
        return j

    def manual_next(self):
        n = len(self.tracks)
        if not n:
            return -1
        if self.loop_mode == self.MODE_SHUFFLE:
            nxt = self._random_index()
            if self.current >= 0:
                self._shuffle_hist.append(self.current)
            return nxt
        return (self.current + 1) % n

    def manual_prev(self):
        n = len(self.tracks)
        if not n:
            return -1
        if self.loop_mode == self.MODE_SHUFFLE:
            if self._shuffle_hist:               # 随机模式下优先回退到随机历史
                return self._shuffle_hist.pop()
            return self._random_index()
        return (self.current - 1) % n

    def auto_next_index(self):
        """自然播完后的下一首；-1 表示整列表结束（顺序播放到尾部）。"""
        n = len(self.tracks)
        if not n:
            return -1
        i = self.current
        if i < 0:
            return 0
        m = self.loop_mode
        if m == self.MODE_REPEAT_ONE:
            return i
        if m == self.MODE_SHUFFLE:
            return self._random_index()
        j = (i + 1) % n
        if j == 0 and m == self.MODE_SEQUENCE:
            return -1
        return j

    # ---------------- 换带判定 ----------------
    def needs_eject(self, old_idx, new_idx):
        """不同专辑或不同文件夹 → 需要"拔出再插入"的换带动画。"""
        if not self.tracks or new_idx < 0:
            return False
        if old_idx < 0:
            return True          # 首次上带：直接走插入动画
        a, b = self.tracks[old_idx], self.tracks[new_idx]
        ka = (a.album.strip().lower(), os.path.dirname(a.path))
        kb = (b.album.strip().lower(), os.path.dirname(b.path))
        album_differs = bool(ka[0]) and bool(kb[0]) and ka[0] != kb[0]
        folder_differs = ka[1] != kb[1]
        return album_differs or folder_differs
