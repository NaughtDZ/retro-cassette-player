"""播放列表读写：M3U/M3U8（通用格式，别的播放器也认）与 JSON 会话（含模式与播放位置）。

- save_m3u / load_playlist：手工"保存播放列表…/打开播放列表…"用。
- save_session / load_session：退出时记住当前列表、当前曲目、播放模式与进度，下次启动自动恢复。
"""
import json
import os

PLAYLIST_FILTER = ("播放列表 (*.m3u8 *.m3u *.json);;M3U8 播放列表 (*.m3u8);;"
                   "M3U 播放列表 (*.m3u);;JSON 列表 (*.json);;所有文件 (*)")


def save_m3u(path, tracks):
    """写标准 M3U8（UTF-8）：#EXTINF 带时长与 "艺术家 - 标题"。"""
    lines = ["#EXTM3U"]
    for t in tracks:
        dur = int(round(t.duration or 0.0))
        label = f"{t.artist} - {t.title}" if t.artist and t.title else (t.title or "")
        lines.append(f"#EXTINF:{dur},{label}")
        lines.append(os.path.abspath(t.path))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def load_m3u(path):
    """读 M3U/M3U8，相对路径按列表文件所在目录解析。"""
    paths = []
    base = os.path.dirname(os.path.abspath(path))
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if not os.path.isabs(s):
                s = os.path.normpath(os.path.join(base, s))
            paths.append(s)
    return paths


def load_playlist(path):
    """按扩展名分派：.json 读 {"tracks": [...]}，其余按 M3U 处理。"""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
        return [p for p in (data.get("tracks") or []) if isinstance(p, str)]
    return load_m3u(path)


def split_existing(paths):
    """拆成 (存在的文件, 缺失的文件) —— 打开列表时跳过已被移动/删除的曲目。"""
    ok, missing = [], []
    seen = set()
    for p in paths:
        p = str(p)
        if p in seen:
            continue
        seen.add(p)
        (ok if os.path.isfile(p) else missing).append(p)
    return ok, missing


def save_session(path, tracks, current, loop_mode, position, volume=None, tape=None):
    """原子写入会话（临时文件 + 替换，避免半截文件）。

    曲目条目带上已探明的元数据快照：上千首的列表下次启动直接回填，
    不必再对每个文件跑一遍 ffprobe（否则启动后会有很长一段时间在后台扫盘）。
    """
    data = {
        "version": 2,
        "tracks": [_track_entry(t) for t in tracks],
        "current": int(current),
        "loop_mode": int(loop_mode),
        "position": round(float(position or 0.0), 3),
    }
    if volume is not None:
        data["volume"] = round(max(0.0, min(1.0, float(volume))), 3)
    if tape:                                  # 磁带 console 上的参数，下次启动原样回来
        data["tape"] = {str(k): round(float(v), 4) for k, v in dict(tape).items()}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _track_entry(t):
    """Track 对象或纯路径 → 会话条目（只存有用的字段，空值不落盘）。"""
    if isinstance(t, (str, os.PathLike)):
        return {"path": str(t)}
    entry = {"path": str(getattr(t, "path", ""))}
    for key in ("title", "artist", "album"):
        v = getattr(t, key, "") or ""
        if v:
            entry[key] = str(v)
    dur = getattr(t, "duration", 0.0) or 0.0
    if dur > 0:
        entry["duration"] = round(float(dur), 3)
    return entry


def read_session_tracks(data):
    """把会话里的 tracks 归一成 [{"path": ..., 可选元数据}]，兼容 v1 的纯路径列表。"""
    out = []
    for item in (data or {}).get("tracks") or []:
        if isinstance(item, str):
            out.append({"path": item})
        elif isinstance(item, dict):
            p = item.get("path")
            if isinstance(p, str) and p:
                out.append(item)
    return out


def load_session(path):
    """读取会话；文件缺失或损坏时返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
