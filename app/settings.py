"""软件配置：集中读写 ``<项目根>/config/settings.json``。

**绝不写用户目录 / 系统盘**：路径只由项目根推导（``config`` 与源码同处一棵树），
换机器、换盘、绿色拷贝都能整体带走。配置本身被 ``.gitignore`` 忽略，不会进 git。

与 ``.cache/session.json`` 的分工：
- ``config/settings.json``：软件偏好（音量、皮肤、效果器参数与开关、窗口位置）
- ``.cache/session.json``：播放会话（列表、当前曲目、播放模式、播放进度）
"""
import json
import os

CONFIG_DIRNAME = "config"
CONFIG_FILENAME = "settings.json"

DEFAULTS = {
    "version": 1,
    "volume": 0.8,
    "skin": "default",
    "fx": {
        "bypass": False,              # 总开关（DSP 级 bypass）
        "params": {},                 # 磁带 DSP 的各个参数
        "modules": {                  # 分区级开关（一键把该分区参数置中性）
            "input": True,
            "transport": True,
            "output": True,
            "repro_eq": True,
        },
    },
    "window": {
        "deck": None,                 # [x, y]，None = 居中
        "console": None,
        "console_visible": False,
        "playlist_topmost": True,
        "playlist_visible": False,
    },
}


def config_dir(root):
    return os.path.join(root, CONFIG_DIRNAME)


def config_path(root):
    return os.path.join(config_dir(root), CONFIG_FILENAME)


def _merge(base, extra):
    """把 extra 深合并进 base 的副本（只认 base 已有的键结构）。"""
    out = json.loads(json.dumps(base))          # 深拷贝默认值
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(root):
    """读配置；文件不存在或损坏时返回默认值（不抛异常）。"""
    path = config_path(root)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("配置根不是对象")
        return _merge(DEFAULTS, data)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULTS))
    except Exception as e:
        print(f"[config] 读取 {path} 失败，改用默认配置：{e}")
        return json.loads(json.dumps(DEFAULTS))


def save(root, data):
    """原子写入配置（临时文件 + 替换，避免半截文件）。"""
    d = config_dir(root)
    os.makedirs(d, exist_ok=True)
    path = config_path(root)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[config] 写入 {path} 失败：{e}")
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def window_pos(data, key):
    """取出窗口位置（[x, y]）或 None。"""
    pos = (data.get("window") or {}).get(key)
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        try:
            return int(pos[0]), int(pos[1])
        except (TypeError, ValueError):
            return None
    return None
