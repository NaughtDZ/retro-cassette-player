"""媒体信息：ffprobe 读取元数据，ffmpeg 抽取内嵌封面（带磁盘缓存）。"""
import hashlib
import json
import os
import re
import subprocess

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QPixmap

from .proc_util import run_hidden

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma"}


def clean_title(stem: str) -> str:
    """去掉文件名开头的曲目序号，如 '01 - Name' / '02.Name'。"""
    s = re.sub(r"^\s*\d{1,3}\s*[-._]\s*", "", stem)
    s = re.sub(r"^\s*\d{2}\.\s+", "", s)
    return s.strip() or stem


def probe_info(ffprobe_exe: str, path: str) -> dict:
    """返回 {title, artist, album, duration}；失败时回退到文件名。"""
    info = {
        "title": clean_title(os.path.splitext(os.path.basename(path))[0]),
        "artist": "",
        "album": "",
        "duration": 0.0,
    }
    try:
        out = run_hidden(
            [ffprobe_exe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=20)
        data = json.loads(out.stdout.decode("utf-8", "ignore")) if out.stdout else {}
    except Exception as e:
        print(f"[probe] {path}: {e}")
        return info

    tags = dict((data.get("format") or {}).get("tags") or {})
    for s in data.get("streams", []):
        if s.get("tags"):
            merged = {**s["tags"], **tags}
            tags = merged
            break

    def pick(*keys):
        for k in keys:
            for kk, vv in tags.items():
                if str(kk).lower() == k and str(vv).strip():
                    return str(vv).strip()
        return ""

    info["title"] = pick("title") or info["title"]
    artist = pick("artist", "composer", "performer")
    album_artist = pick("albumartist", "album artist")
    info["artist"] = f"{artist} / {album_artist}".strip(" /") if artist and album_artist else (artist or album_artist)
    info["album"] = pick("album")

    dur = 0.0
    try:
        dur = float((data.get("format") or {}).get("duration", "0"))
    except Exception:
        pass
    if not dur:
        for s in data.get("streams", []):
            try:
                dur = max(dur, float(s.get("duration", "0")))
            except Exception:
                continue
    info["duration"] = round(dur, 3)
    return info


def extract_cover(ffmpeg_exe: str, path: str, cache_dir: str):
    """抽取内嵌封面到缓存目录；返回 (png_path|None)。"""
    try:
        key_src = f"{path}|{os.path.getmtime(path)}"
    except OSError:
        return None
    h = hashlib.sha1(key_src.encode("utf-8", "ignore")).hexdigest()
    out_png = os.path.join(cache_dir, f"{h}.png")
    if os.path.isfile(out_png) and os.path.getsize(out_png) > 100:
        return out_png
    try:
        run_hidden(
            [ffmpeg_exe, "-v", "error", "-i", str(path),
             "-map", "0:v?", "-frames:v", "1", "-y", out_png],
            capture_output=True, timeout=30)
    except Exception as e:
        print(f"[cover] {path}: {e}")
        return None
    if os.path.isfile(out_png) and os.path.getsize(out_png) > 100:
        return out_png
    try:
        if os.path.exists(out_png):
            os.remove(out_png)
    except OSError:
        pass
    return None


class ProbeWorker(QThread):
    """批量探测元数据（逐条发信号，便于增量刷新播放列表）。"""
    one = Signal(str, object)     # path -> meta dict
    done = Signal()

    def __init__(self, ffprobe_exe, paths, parent=None):
        super().__init__(parent)
        self.ffprobe_exe = ffprobe_exe
        self.paths = list(paths)
        self._cancel = False

    def cancel(self):
        """请求尽快收尾（退出程序时调用，避免带着运行中的线程退进程）。"""
        self._cancel = True

    def run(self):
        for p in self.paths:
            if self._cancel:
                break
            try:
                self.one.emit(p, probe_info(self.ffprobe_exe, p))
            except Exception as e:      # 线程里任何异常都不该把整个进程带走
                print(f"[probe] emit 失败 {p}: {e}")
        self.done.emit()


class CoverWorker(QThread):
    """抽取单条曲目封面（含缓存），完成后发 QPixmap 或 None。"""
    ready = Signal(str, object)   # path -> QPixmap | None

    def __init__(self, ffmpeg_exe, cache_dir, path, parent=None):
        super().__init__(parent)
        self.ffmpeg_exe = ffmpeg_exe
        self.cache_dir = cache_dir
        self.path = str(path)

    def run(self):
        png = extract_cover(self.ffmpeg_exe, self.path, self.cache_dir)
        pm = None
        if png:
            pm = QPixmap(png)
            if pm.isNull():
                pm = None
        self.ready.emit(self.path, pm)


def scan_folder(dir_path: str):
    """递归扫描目录下的音频文件（按路径排序）。"""
    found = []
    for base, _dirs, files in os.walk(dir_path):
        for f in sorted(files):
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                found.append(os.path.join(base, f))
    return sorted(found)
