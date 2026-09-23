"""皮肤包加载：skins/<name>/skin.json + svg/png 资源。

每个部件（底座/磁带壳/按钮/占位封面）都可以被同名资源覆盖；
缺失的资源回退到内置矢量绘制，因此任何不完整的皮肤包都能安全运行。
"""
import json
import os

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QByteArray
from PySide6.QtGui import QPainter, QPixmap, QColor
from PySide6.QtSvg import QSvgRenderer


# ---------------- 默认几何（窗口 960x620）与配色 ----------------
DEFAULT_LAYOUT = {
    "tape":      (220, 142, 520, 356),   # 底边 y=498，落在底座上沿(468)之下 → 有"插进槽位"的观感
    "base":      (76, 468, 808, 132),
    "btn_prev":  (238, 516, 46, 46),
    "btn_play":  (312, 516, 46, 46),
    "btn_next":  (386, 516, 46, 46),
    "btn_stop":  (460, 516, 46, 46),
    "btn_loop":  (560, 516, 46, 46),
    "win_min":   (676, 520, 34, 34),
    "win_menu":  (718, 520, 34, 34),
    "win_list":  (760, 520, 34, 34),
    "win_close": (802, 520, 34, 34),
    "knob":      (134, 508, 64, 64),     # 音量旋钮（底座左端，窗口坐标）
    "vu_left":   (46, 296, 148, 96),     # 主界面左声道 VU 表（磁带左侧机身）
    "vu_right":  (766, 296, 148, 96),    # 右声道 VU 表
    # 呼出磁带 console 悬浮窗。它与窗口键组之间要留出足够间隙：底座上这两组各画一块
    # 下沉面板，间距不够时两块凹槽会互相穿插（svg 里只有 6px 是放不下两边各 6px 余量的）。
    "btn_console": (596, 516, 46, 46),
}

DEFAULT_COLORS = {
    "bg": "#f2ecdf",            # 桌面背景
    "base": "#2a6485",          # 底座主体
    "base_edge": "#173c52",     # 底座描边
    "base_lip": "#4d8db0",      # 底座顶部高光
    "btn_bg": "#ffd23f",        # 控制按钮底色（黄）
    "btn_hover": "#ffe06e",
    "btn_glyph": "#23282e",     # 按钮图标色
    "chrome_bg": "#1c4a66",     # 窗口小按钮底色
    "chrome_hover": "#2d6285",
    "chrome_glyph": "#dfe9f0",
    "tape_shell": "#20536f",    # 磁带壳
    "tape_edge": "#11344a",
    "tape_screw": "#0e2a3b",
    "reel_disc": "#f7f2e7",     # 滚轴盘面
    "reel_hub": "#1a4258",      # 滚轴轮毂
    "reel_spoke": "#9aa7b1",    # 滚轴辐条
    "text_title": "#2f3742",    # 标签纸上的标题（白纸深字）
    "text_sub": "#6a7480",      # 副标题
    "lcd_bg": "#0e1d16",        # 计数器 LCD 底
    "lcd_fg": "#8dffab",        # LCD 数字色
    # ---- 磁带 console 悬浮窗（面板 / 旋钮 / 挡位旋钮）----
    "console_bg_top": "#3b3a36",
    "console_bg": "#2e2d2a",
    "console_bg_bottom": "#232220",
    "console_edge": "#15140f",
    "console_zone": "#3a3934",
    "console_zone_line": "#55534c",
    "console_label": "#cfc4a8",
    "knob_face": "#cfc7b4",
    "knob_rim": "#6d6656",
    "knob_pointer": "#d8c79a",
    "knob_mark": "#241d14",
    "switch_face": "#5d5a52",
}


class Skin:
    def __init__(self, name="builtin", pack_dir=None):
        self.name = name
        self.colors = dict(DEFAULT_COLORS)
        self.layout = {k: tuple(v) for k, v in DEFAULT_LAYOUT.items()}
        self.assets = {}        # key -> ("svg", QSvgRenderer) | ("pix", QPixmap)
        self._pm_cache = {}     # key -> QPixmap：SVG 的光栅缓存（避免每帧重新光栅化）
        if pack_dir and os.path.isdir(pack_dir):
            self._load_pack(pack_dir)

    def _load_pack(self, d, _depth=0):
        if _depth > 3:                       # 防止 base_pack 互为父包时无限递归
            return
        jf = os.path.join(d, "skin.json")
        data = {}
        if os.path.isfile(jf):
            try:
                with open(jf, encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as e:
                print(f"[skin] {jf} 解析失败：{e}")
        # 继承：先加载 base_pack（通常为 default），本包再覆盖
        parent = data.get("base_pack")
        if parent:
            pdir = os.path.join(os.path.dirname(os.path.abspath(d)), str(parent))
            if os.path.isdir(pdir) and os.path.abspath(pdir) != os.path.abspath(d):
                self._load_pack(pdir, _depth + 1)
            else:
                print(f"[skin] base_pack 不存在：{pdir}")
        for k, v in (data.get("colors") or {}).items():
            if isinstance(v, str):
                self.colors[k] = v
        for k, rect in (data.get("layout") or {}).items():
            try:
                x, y, w, h = rect
                self.layout[k] = (int(x), int(y), int(w), int(h))
            except Exception:
                continue
        for key, fname in (data.get("assets") or {}).items():
            if not fname:                 # 空值：丢掉继承来的资源，改用程序矢量绘制（如 base: ""）
                self.assets.pop(key, None)
                continue
            p = os.path.join(d, str(fname))
            if not os.path.isfile(p):
                print(f"[skin] 资源缺失：{p}")
                continue
            try:
                if str(fname).lower().endswith(".svg"):
                    r = QSvgRenderer()
                    with open(p, "rb") as f:
                        ok = r.load(QByteArray(f.read()))
                    if ok:
                        self.assets[key] = ("svg", r)
                else:
                    pm = QPixmap(p)
                    if not pm.isNull():
                        self.assets[key] = ("pix", pm)
            except Exception as e:
                print(f"[skin] 资源加载失败 {p}: {e}")

    # ---------------- 查询 ----------------
    def color(self, key):
        return QColor(self.colors.get(key, "#888888"))

    def rect(self, key):
        try:
            x, y, w, h = self.layout[key]
            return QRect(x, y, int(w), int(h))
        except (KeyError, TypeError, ValueError):
            return None

    # ---------------- 绘制 ----------------
    def _asset_pixmap(self, key, size):
        """把 SVG 资源按尺寸光栅化成位图并缓存：动画帧只贴图，不再逐帧矢量光栅化。"""
        pm = self._pm_cache.get(key)
        if pm is not None and pm.size() == size:
            return pm
        renderer = self.assets[key][1]
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.Antialiasing)
        renderer.render(pt, QRectF(QRect(QPoint(0, 0), size)))
        pt.end()
        self._pm_cache[key] = pm
        return pm

    def asset_size(self, key, fallback=(520, 356)):
        """给调用方推断资源画布尺寸用（SVG 取其 viewBox）。"""
        a = self.assets.get(key)
        if a is None:
            return QSize(*fallback)
        if a[0] == "svg":
            d = a[1].defaultSize()
            if d.isValid() and d.width() > 0:
                return d
        else:
            return a[1].size()
        return QSize(*fallback)

    def draw_asset(self, p, key, rect, fallback=None):
        """优先画皮肤资源（SVG 走光栅缓存）；缺失/失败时调用 fallback(p) 矢量兜底。"""
        a = self.assets.get(key)
        if a is not None and rect is not None:
            kind, obj = a
            try:
                if kind == "svg":
                    size = QSize(int(rect.width()), int(rect.height()))
                    p.drawPixmap(QPointF(rect.left(), rect.top()), self._asset_pixmap(key, size))
                else:
                    pm = obj.scaled(rect.size(), 1, 2)   # KeepAspectRatio + Smooth
                    x = rect.x() + (rect.width() - pm.width()) // 2
                    y = rect.y() + (rect.height() - pm.height()) // 2
                    p.drawPixmap(x, y, pm)
                return True
            except Exception as e:
                print(f"[skin] 绘制 {key} 失败：{e}")
        if fallback is not None:
            fallback(p)
        return False

    # ---------------- 资源优先 / 程序回退 的统一入口 ----------------
    # v1.1.0 起，**所有**绘制的图形都可以放进皮肤包：静态底图直接用资源，状态变体用后缀键，
    # 随数值连续变化的元素（指针角度、卷径、进度填充）则约定成"资源 + 变换"。
    # 任何一个 key 缺失或写成 "" 都会回退到程序矢量绘制，旧皮肤零改动可用。
    STATE_SUFFIX = {"hover": "_hover", "down": "_down", "on": "_on", "off": "_off"}

    def has_asset(self, key):
        return bool(key) and key in self.assets

    def resolve(self, key, state=None):
        """按状态解析出真正要用的资源键；没有资源时返回 None。"""
        if state:
            alt = key + self.STATE_SUFFIX.get(state, "")
            if alt in self.assets:
                return alt
        return key if key in self.assets else None

    def draw_part(self, p, key, rect, state=None, fallback=None):
        """带状态变体的资源绘制：有图用图，没有就交给 fallback（程序矢量）。"""
        k = self.resolve(key, state)
        if k is None:
            if fallback is not None:
                fallback(p)
            return False
        return self.draw_asset(p, k, rect, fallback)

    def _part_pixmap(self, key, rect):
        a = self.assets.get(key)
        if a is None or rect is None:
            return None
        size = QSize(max(1, int(rect.width())), max(1, int(rect.height())))
        if a[0] == "svg":
            return self._asset_pixmap(key, size)
        return a[1].scaled(size, 1, 2)

    def draw_rotated(self, p, key, rect, angle_deg, fallback=None):
        """把资源绕**图片中心**旋转后绘制（旋钮指针 / VU 指针）。

        约定：指针图按 0° 画成"朝上"，中心即转轴；程序按数值换算角度。
        """
        pm = self._part_pixmap(key, rect)
        if pm is None:
            if fallback is not None:
                fallback(p)
            return False
        c = QPointF(rect.center())
        p.save()
        p.translate(c)
        p.rotate(float(angle_deg))
        p.drawPixmap(QPointF(-pm.width() / 2.0, -pm.height() / 2.0), pm)
        p.restore()
        return True

    def draw_scaled(self, p, key, rect, scale, fallback=None):
        """按比例缩放绘制、中心对齐（磁带卷径随进度；约定图 = 最大状态）。"""
        pm = self._part_pixmap(key, rect)
        if pm is None or scale <= 0:
            if fallback is not None:
                fallback(p)
            return False
        w, h = pm.width() * float(scale), pm.height() * float(scale)
        p.drawPixmap(QPointF(rect.center().x() - w / 2.0, rect.center().y() - h / 2.0),
                     pm.scaled(int(max(1, w)), int(max(1, h)), 1, 2))
        return True

    def draw_clipped(self, p, key, rect, frac, fallback=None):
        """按水平比例裁剪绘制（进度条填充；约定图 = 满值状态，从左向右露出）。"""
        pm = self._part_pixmap(key, rect)
        if pm is None:
            if fallback is not None:
                fallback(p)
            return False
        frac = max(0.0, min(1.0, float(frac)))
        if frac <= 0.0:
            return True
        w = int(round(pm.width() * frac))
        if w <= 0:
            return True
        p.drawPixmap(QPointF(rect.left(), rect.top()), pm, QRectF(0, 0, w, pm.height()))
        return True

    @staticmethod
    def list_packs(skins_dir):
        names = []
        if os.path.isdir(skins_dir):
            for n in sorted(os.listdir(skins_dir)):
                d = os.path.join(skins_dir, n)
                if os.path.isdir(d) and any(
                        f == "skin.json" or f.lower().endswith((".svg", ".png"))
                        for _f, f in ((0, x) for x in os.listdir(d))):
                    names.append(n)
        return names
