"""悬浮歌词窗。

歌词源：与音频同目录、同名的 `.lrc`（支持 `[mm:ss.xx]`、一行多时间标签、`[offset:]`）。
没有 .lrc 时显示提示，不影响播放。

渲染：当前行居中大字，上下各一行淡色；随播放位置滚动，滚动量做了平滑。
交互：
  - 空白处按住拖动移动
  - 右下角手柄拖动缩放，**字号 = 基准字号 × 高度比**，所以缩放窗口时字自动跟着变
  - 右键菜单：字体 / 字号 / 字色、背景（纯色+透明度 / 图片三模式）、锁定位置、关闭
  - 锁定后不能拖动与缩放，但仍可右键解锁或改样式
"""
import os
import re

from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QColorDialog, QFileDialog, QFontDialog, QInputDialog,
                              QMenu, QWidget)

# ---- 字幕解析：LRC / SRT / VTT / ASS / SSA ---------------------------------
SUBTITLE_EXTS = (".lrc", ".srt", ".vtt", ".ass", ".ssa")

_TIME_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_OFFSET_TAG = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
# SRT: 00:00:01,000 --> 00:00:04,500   VTT: 00:01.000 --> 00:04.500（小时可省）
_SRT_TIME = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")
_ASS_TAG = re.compile(r"\{[^}]*\}")          # {\i1} {\pos(..)} 之类


def _hms(h, m, s, ms):
    return (int(h or 0) * 3600 + int(m) * 60 + int(s)
            + int(str(ms).ljust(3, "0")) / 1000.0)


def _parse_lrc(text):
    """LRC：支持 [mm:ss.xx]、一行多时间标签、[offset:毫秒]。"""
    offset = 0.0
    m = _OFFSET_TAG.search(text)
    if m:
        offset = int(m.group(1)) / 1000.0
    out = []
    for line in text.splitlines():
        tags = _TIME_TAG.findall(line)
        if not tags:
            continue
        body = _TIME_TAG.sub("", line).strip()
        for mm, ss in tags:
            try:
                t = int(mm) * 60.0 + float(ss.replace(":", ".")) + offset
            except ValueError:
                continue
            out.append((max(0.0, t), body))
    out.sort(key=lambda x: x[0])
    # LRC 没有结束时间：下一句开始即本句结束（末句给 8 秒）
    res = []
    for i, (t, body) in enumerate(out):
        end = out[i + 1][0] if i + 1 < len(out) else t + 8.0
        res.append((t, end, body))
    return res


def _parse_srt(text):
    """SRT / VTT：按 '-->' 时间行切块，取紧随其后的文本块。"""
    out = []
    for m in _SRT_TIME.finditer(text):
        g = m.groups()
        start = _hms(g[0], g[1], g[2], g[3])
        end = _hms(g[4], g[5], g[6], g[7])
        rest = text[m.end():]
        block = rest.split("\n\n", 1)[0]
        lines = []
        for ln in block.strip().splitlines():
            s = ln.strip()
            if not s:
                continue
            if _SRT_TIME.search(s):        # 下一个 cue 的时间行，到此为止
                break
            if s.isdigit() and not lines:  # 序号行
                continue
            lines.append(s)
        body = " ".join(lines)
        if body:
            out.append((start, end, body))
    out.sort(key=lambda x: x[0])
    return out


def _ass_time(s):
    """ASS 时间 0:00:01.00 -> 秒。"""
    try:
        h, m, sec = s.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, AttributeError):
        return 0.0


def _parse_ass(text):
    """ASS / SSA：只读 [Events] 段的 Dialogue 行，剥掉 {\\...} 样式标签。"""
    out = []
    in_events = False
    for ln in text.splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith("[") and low.endswith("]"):
            in_events = (low == "[events]")
            continue
        if not in_events or not low.startswith("dialogue:"):
            continue
        # 前 9 个逗号切开；**最后一段一定是 Text**（后面还有逗号也都是正文）。
        # 字段数在 ASS v4+ / SSA v4 之间不一致（Layer vs Marked、是否带 MarginV），
        # 所以只依赖位置固定的 Start/End(parts[1]/[2]) 和 parts[-1]，不数字段总数。
        parts = s.split(",", 9)
        if len(parts) < 4:
            continue
        body = _ASS_TAG.sub("", parts[-1])
        body = (body.replace("\\N", " ").replace("\\n", " ")
                    .replace("\\h", " ").strip())
        if body:
            out.append((_ass_time(parts[1]), _ass_time(parts[2]), body))
    out.sort(key=lambda x: x[0])
    return out


def parse_subtitle(path):
    """按扩展名解析字幕，统一返回 [(开始秒, 结束秒, 文本)]，失败返回 []。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    ext = os.path.splitext(path)[1].lower()
    if ext == ".lrc":
        return _parse_lrc(text)
    if ext in (".srt", ".vtt"):
        return _parse_srt(text)
    if ext in (".ass", ".ssa"):
        return _parse_ass(text)
    return []


# ---- 自动嗅探字幕 ------------------------------------------------------------
_HINT_BYTES = 96 * 1024          # 字幕文件都很小，前 96KB 基本等于全文


def _content_mentions(path, needles):
    """字幕文件内容里是否出现这些名字（大小写不敏感）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(_HINT_BYTES)
    except OSError:
        return False
    low = head.decode("utf-8", "ignore").lower()
    return any(n and n.lower() in low for n in needles)


def find_subtitle(audio_path, title=None):
    """在音频同目录里找它的字幕。

    匹配优先级（越靠前越可信）：
      1. 文件名主体与音频同名（同名 + 任意支持的字幕扩展名）
      2. 字幕文件内容里出现音频名 / 标题（文件名不一致也能认领）
      3. 目录里只有唯一一个字幕文件
    多个候选取"优先级最高 + 修改时间最新"的。找不到返回 None。
    """
    if not audio_path:
        return None
    folder = os.path.dirname(os.path.abspath(audio_path))
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    full = os.path.basename(audio_path)
    needles = [n for n in (stem, full, title or "") if n]
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    subs = [os.path.join(folder, n) for n in names
            if os.path.splitext(n)[1].lower() in SUBTITLE_EXTS
            and os.path.isfile(os.path.join(folder, n))]
    if not subs:
        return None
    ranked = []
    for p in subs:
        stem_p = os.path.splitext(os.path.basename(p))[0]
        if stem_p.lower() == stem.lower():
            rank = 0
        elif _content_mentions(p, needles):
            rank = 1
        else:
            rank = 2
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
        ranked.append((rank, -mtime, p))
    ranked.sort()
    # "唯一字幕"兜底也算可用，所以直接返回最优的一个
    return ranked[0][2]


DEFAULTS = {
    "enabled": False,
    "font_family": "",          # 空 = 用程序默认字体
    "font_pt": 22.0,            # 基准字号（在基准高度下）
    "text_color": "#ffffff",
    "dim_color": "#ffffff",     # 非当前行（会再乘透明度）
    "bold": True,
    "bg_mode": "solid",         # solid | image
    "bg_color": "#000000",
    "bg_alpha": 110,            # 0-255
    "image_path": "",
    "image_fit": "crop",        # crop | stretch | fit
    "locked": False,
    "pos": None,                # [x, y]
    "size": None,               # [w, h]
    "topmost": True,
}


class LyricWindow(QWidget):
    """自绘的透明悬浮歌词窗。"""

    BASE_W, BASE_H = 620, 160        # 基准尺寸：字号按"当前高度 / BASE_H"缩放
    BASE_PT = 22.0                   # 基准高度下的字号
    MIN_W, MIN_H = 260, 96
    HANDLE = 22                      # 右下角缩放手柄边长

    def __init__(self, conf=None, on_change=None, parent=None):
        super().__init__(parent)
        self.conf = dict(DEFAULTS)
        if conf:
            self.conf.update({k: v for k, v in conf.items() if k in DEFAULTS})
        self.on_change = on_change
        self.lines = []              # [(开始秒, 结束秒, 文本)]
        self.subtitle_path = None    # 当前嗅探到的字幕文件（调试/提示用）
        self._pos = 0.0              # 播放位置（秒）
        self._idx = -1               # 当前行序号
        self._scroll = 0.0           # 平滑滚动偏移（行）
        self._drag_off = None
        self._resizing = False
        self._resize_from = None
        self._pixmap = None
        self._pixmap_path = None

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setMinimumSize(self.MIN_W, self.MIN_H)

        w, h = (self.conf.get("size") or [self.BASE_W, self.BASE_H])[:2]
        self.resize(max(self.MIN_W, int(w)), max(self.MIN_H, int(h)))

        f = self.font()
        f.setPointSizeF(self.BASE_PT)
        f.setBold(True)
        self.setFont(f)

        self._tick = QTimer(self)
        self._tick.setInterval(40)
        self._tick.timeout.connect(self._animate)
        self._tick.start()

    # ---------------- 对外接口 ----------------
    def set_track(self, audio_path, title=None):
        """切歌时调用：在音频目录里嗅探字幕（LRC/SRT/VTT/ASS/SSA）并解析。"""
        self.subtitle_path = find_subtitle(audio_path, title)
        self.lines = parse_subtitle(self.subtitle_path) if self.subtitle_path else []
        self._idx = -1
        self._scroll = 0.0
        self.update()

    def set_position(self, sec):
        self._pos = max(0.0, float(sec or 0.0))

    def set_conf(self, **kw):
        self.conf.update({k: v for k, v in kw.items() if k in DEFAULTS})
        if "image_path" in kw:
            self._pixmap = None          # 换了图，下次绘制重新载入
        self._apply_flags()
        self.update()
        self._emit()

    def apply(self, conf):
        """批量套用配置（启动恢复用）。"""
        self.conf.update({k: v for k, v in (conf or {}).items() if k in DEFAULTS})
        self._pixmap = None
        w, h = (self.conf.get("size") or [self.BASE_W, self.BASE_H])[:2]
        self.resize(max(self.MIN_W, int(w)), max(self.MIN_H, int(h)))
        self._apply_flags()
        self.update()

    def current_index(self):
        if not self.lines:
            return -1
        lo, hi, ans = 0, len(self.lines) - 1, -1
        while lo <= hi:                  # 二分找"最后一个时间 <= 当前位置"
            mid = (lo + hi) // 2
            if self.lines[mid][0] <= self._pos + 0.02:
                ans = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    # ---------------- 内部 ----------------
    def _emit(self):
        if self.on_change:
            self.on_change(self.snapshot())

    def snapshot(self):
        c = dict(self.conf)
        c["size"] = [self.width(), self.height()]
        c["pos"] = [self.x(), self.y()]
        return c

    def _apply_flags(self):
        top = bool(self.conf.get("topmost", True))
        want = Qt.FramelessWindowHint | Qt.Tool
        want |= (Qt.WindowStaysOnTopHint if top else Qt.WindowType(0))
        if self.windowFlags() != want:
            was = self.isVisible()
            self.setWindowFlags(want)
            if was:
                self.show()

    def font_pt(self):
        """字号随窗口高度缩放——这就是"缩放窗口时字自动跟着变"。"""
        base = float(self.conf.get("font_pt") or self.BASE_PT)
        return max(6.0, base * self.height() / float(self.BASE_H))

    def _load_pixmap(self):
        path = self.conf.get("image_path") or ""
        if path == self._pixmap_path:
            return self._pixmap
        self._pixmap_path = path
        pm = QPixmap(path) if path and os.path.isfile(path) else None
        self._pixmap = pm if (pm is not None and not pm.isNull()) else None
        return self._pixmap

    def _animate(self):
        idx = self.current_index()
        if idx != self._idx:
            self._idx = idx
        # 滚动偏移朝当前行收敛（0.25 的阻尼，看起来是"滑"过去而不是跳）
        target = float(max(0, idx))
        if abs(target - self._scroll) > 0.002:
            self._scroll += (target - self._scroll) * 0.25
            self.update()
        elif self.lines and self.isVisible():
            # 当前行不变也可能因暂停/拖动而需要重画
            self.update()

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        p.setClipPath(path)

        # ---- 背景 ----
        if (self.conf.get("bg_mode") or "solid") == "image" and self._load_pixmap():
            self._draw_image_bg(p, self._pixmap, r, self.conf.get("image_fit") or "crop")
        else:
            c = QColor(self.conf.get("bg_color") or "#000000")
            c.setAlpha(max(0, min(255, int(self.conf.get("bg_alpha", 110)))))
            p.fillRect(r, c)

        # ---- 歌词 ----
        if not self.lines:
            self._draw_hint(p, r)
            p.end()
            return
        self._draw_lines(p, r)

        if not self.conf.get("locked"):
            self._draw_handle(p, r)
        p.end()

    def _draw_image_bg(self, p, pm, r, mode):
        """三种图片铺法：stretch 拉伸变形、crop 裁切填满、fit 完整放入。"""
        W, H = r.width(), r.height()
        iw, ih = pm.width(), pm.height()
        if iw <= 0 or ih <= 0:
            return
        if mode == "stretch":
            p.drawPixmap(r, pm, QRectF(0, 0, iw, ih))
            return
        scale = (W / iw) if mode == "crop" else min(W / iw, H / ih)
        if mode == "crop":
            scale = max(W / iw, H / ih)
        dw, dh = iw * scale, ih * scale
        dx, dy = (W - dw) / 2.0, (H - dh) / 2.0
        p.drawPixmap(QRectF(dx, dy, dw, dh), pm, QRectF(0, 0, iw, ih))

    def _text_font(self):
        f = QFont()
        fam = self.conf.get("font_family") or ""
        if fam:
            f.setFamily(fam)
        f.setPointSizeF(self.font_pt())
        f.setBold(bool(self.conf.get("bold", True)))
        return f

    def _draw_lines(self, p, r):
        f = self._text_font()
        p.setFont(f)
        fm = p.fontMetrics()
        line_h = fm.height() * 1.45
        cx = r.center().x()
        cy = r.center().y()
        base_col = QColor(self.conf.get("text_color") or "#ffffff")
        dim_col = QColor(self.conf.get("dim_color") or "#ffffff")

        cur = self._idx if self._idx >= 0 else 0
        # 当前行居中；上下各一行。_scroll 让切换时平滑过渡
        offset = (cur - self._scroll) * line_h
        for delta, alpha in ((-1, 0.45), (0, 1.0), (1, 0.45)):
            i = cur + delta
            if not (0 <= i < len(self.lines)):
                continue
            text = self.lines[i][2] or "♪"
            col = QColor(base_col if delta == 0 else dim_col)
            col.setAlphaF(alpha * (1.0 if delta == 0 else 0.75))
            p.setPen(col)
            y = cy + offset + delta * line_h - line_h / 2.0
            rect = QRectF(r.left() + 14, y, r.width() - 28, line_h)
            shown = fm.elidedText(text, Qt.ElideRight, int(rect.width()))
            p.drawText(rect, Qt.AlignHCenter | Qt.AlignVCenter, shown)

    def _draw_hint(self, p, r):
        f = self._text_font()
        f.setPointSizeF(max(8.0, self.font_pt() * 0.52))
        p.setFont(f)
        c = QColor(self.conf.get("text_color") or "#ffffff")
        c.setAlphaF(0.55)
        p.setPen(c)
        p.drawText(r, Qt.AlignCenter, "未找到字幕文件（.lrc/.srt/.vtt/.ass/.ssa）")

    def _draw_handle(self, p, r):
        h = self.HANDLE
        rect = QRectF(r.right() - h, r.bottom() - h, h, h)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1.0))
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 3, 3)
        p.setPen(QPen(QColor(255, 255, 255, 120), 1.2))
        for i in (0, 1, 2):
            o = 6 + i * 4
            p.drawLine(r.right() - o, r.bottom() - 5, r.right() - 5, r.bottom() - o)

    # ---------------- 交互 ----------------
    def _in_handle(self, pos):
        return pos.x() >= self.width() - self.HANDLE and pos.y() >= self.height() - self.HANDLE

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.conf.get("locked"):
            return                       # 锁定：不拖不缩，但右键菜单仍可用
        pos = e.position().toPoint()
        if self._in_handle(pos):
            self._resizing = True
            self._resize_from = (self.width(), self.height(), e.globalPosition().toPoint())
        else:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        e.accept()

    def mouseMoveEvent(self, e):
        if self.conf.get("locked"):
            return
        gp = e.globalPosition().toPoint()
        if self._resizing and self._resize_from:
            w0, h0, origin = self._resize_from
            dw = gp.x() - origin.x()
            dh = gp.y() - origin.y()
            self.resize(max(self.MIN_W, w0 + dw), max(self.MIN_H, h0 + dh))
            e.accept()
            return
        if self._drag_off is not None:
            self.move(gp - self._drag_off)
            e.accept()
            return
        # 悬停时给缩放手柄一个提示光标
        if self._in_handle(e.position().toPoint()):
            self.setCursor(Qt.SizeFDiagCursor)
        else:
            self.setCursor(Qt.OpenHandCursor)

    def mouseReleaseEvent(self, e):
        changed = self._resizing or (self._drag_off is not None)
        self._resizing = False
        self._resize_from = None
        self._drag_off = None
        if changed:
            self._emit()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a_font = m.addAction("字体…")
        a_color = m.addAction("文字颜色…")
        a_dim = m.addAction("非当前行颜色…")
        a_pt = m.addAction("字号…")
        m.addSeparator()
        sub_bg = QMenu("背景", m)
        a_bg_solid = sub_bg.addAction("纯色…")
        a_bg_alpha = sub_bg.addAction("背景不透明度…")
        a_bg_img = sub_bg.addAction("选择图片…")
        a_bg_clear = sub_bg.addAction("清除图片")
        sub_fit = sub_bg.addMenu("图片填充方式")
        acts_fit = {k: sub_fit.addAction(v) for k, v in
                    (("crop", "裁切填满"), ("stretch", "拉伸填满"), ("fit", "适应大小"))}
        for k, a in acts_fit.items():
            a.setCheckable(True)
            a.setChecked((self.conf.get("image_fit") or "crop") == k)
        m.addMenu(sub_bg)
        m.addSeparator()
        a_lock = m.addAction("锁定位置 / 大小")
        a_lock.setCheckable(True)
        a_lock.setChecked(bool(self.conf.get("locked")))
        a_top = m.addAction("置顶显示")
        a_top.setCheckable(True)
        a_top.setChecked(bool(self.conf.get("topmost", True)))
        m.addSeparator()
        a_close = m.addAction("关闭歌词窗")

        act = m.exec(e.globalPos())
        if act is None:
            return
        if act == a_font:
            self._pick_font()
        elif act == a_color:
            c = QColorDialog.getColor(QColor(self.conf.get("text_color") or "#ffffff"),
                                      self, "文字颜色")
            if c.isValid():
                self.set_conf(text_color=c.name(), dim_color=c.name())
        elif act == a_dim:
            c = QColorDialog.getColor(QColor(self.conf.get("dim_color") or "#ffffff"),
                                      self, "非当前行颜色")
            if c.isValid():
                self.set_conf(dim_color=c.name())
        elif act == a_pt:
            pt, ok = QInputDialog.getDouble(self, "字号", "基准字号 (pt)：",
                                            float(self.conf.get("font_pt") or self.BASE_PT),
                                            6.0, 96.0, 1)
            if ok:
                self.set_conf(font_pt=pt)
        elif act == a_bg_solid:
            c = QColorDialog.getColor(QColor(self.conf.get("bg_color") or "#000000"),
                                      self, "背景颜色")
            if c.isValid():
                self.set_conf(bg_mode="solid", bg_color=c.name())
        elif act == a_bg_alpha:
            v, ok = QInputDialog.getInt(self, "背景不透明度", "0-255（0 完全透明）：",
                                        int(self.conf.get("bg_alpha", 110)), 0, 255, 5)
            if ok:
                self.set_conf(bg_alpha=v)
        elif act == a_bg_img:
            p, _ = QFileDialog.getOpenFileName(self, "选择背景图片", "",
                                               "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
            if p:
                self.set_conf(bg_mode="image", image_path=p)
        elif act == a_bg_clear:
            self.set_conf(bg_mode="solid", image_path="")
        elif act in acts_fit.values():
            for k, a in acts_fit.items():
                if a is act:
                    self.set_conf(image_fit=k)
                    break
        elif act == a_lock:
            self.set_conf(locked=not self.conf.get("locked"))
            self.setCursor(Qt.ArrowCursor if self.conf.get("locked") else Qt.OpenHandCursor)
        elif act == a_top:
            self.set_conf(topmost=not self.conf.get("topmost", True))
        elif act == a_close:
            self.hide()
            self._emit()

    def _pick_font(self):
        f = self._text_font()
        ok, nf = QFontDialog.getFont(f, self, "歌词字体")
        if not ok:
            return
        self.set_conf(font_family=nf.family(),
                      bold=nf.bold(),
                      font_pt=max(6.0, min(96.0, nf.pointSizeF() if nf.pointSizeF() > 0
                                           else self.conf.get("font_pt", self.BASE_PT))))
