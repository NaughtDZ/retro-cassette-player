"""复古磁带 console 悬浮窗：一块实体机面板，控件全部自绘且可换皮肤。

- 面板底、旋钮、挡位旋钮、拨杆都由程序绘制，颜色取自皮肤包的 ``colors``；
  想整块换皮就在皮肤里放一张 ``console_panel``（尺寸 = 面板内容尺寸）。
- 参数直接写进磁带 DSP（经 ``on_param`` 回调到 app 层 → engine.set_tape_param）。
- 交互：旋钮绕圈拧 / 滚轮微调 / 挡位旋钮点击换挡（右键反向）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import QWidget

from .shadow_window import ShadowWindow
from .tape_fx import ENUMS, PARAM_SPEC

SURFACE_W = 480
SURFACE_H = 686
MARGIN = 14
TOP_BAR = 44
ZONE_H = 118
ZONE_GAP = 6

# 分区：(区名, [(参数名, 显示标签, 类型, 单位), ...])
ZONES = [
    ("INPUT 输入", [
        ("input_gain_db", "GAIN", "knob", "dB"),
        ("bias", "BIAS", "knob", ""),
        ("highpass_hz", "HPF", "knob", "Hz"),
        ("lowpass_hz", "LPF", "knob", "Hz"),
    ]),
    ("TAPE 磁带", [
        ("machine", "DECK", "switch", ""),
        ("speed", "SPEED", "switch", ""),
        ("type", "TAPE", "switch", ""),
        ("calibration", "CAL", "switch", "dB"),
    ]),
    ("TRANSPORT 走带", [
        ("wow", "WOW", "knob", "%"),
        ("flutter", "FLUTTER", "knob", "%"),
        ("noise", "NOISE", "knob", "%"),
        ("wowflutter_on", "W/F", "switch", ""),
    ]),
    ("OUTPUT 输出", [
        ("output_gain_db", "GAIN", "knob", "dB"),
        ("auto_comp", "AUTO", "switch", ""),
        ("oversampling", "OS", "switch", ""),
        ("active", "POWER", "switch", ""),
    ]),
    ("REPRO EQ 重放均衡", [
        ("repro_lf", "LF", "knob", "dB"),
        ("repro_lmf", "LMF", "knob", "dB"),
        ("repro_hmf", "HMF", "knob", "dB"),
        ("repro_hf", "HF", "knob", "dB"),
        ("repro_sub", "SUB", "knob", "dB"),
        ("lp_q", "LP Q", "knob", ""),
    ]),
]

SWITCH_LABELS = {
    "auto_comp": ["关", "开"],
    "wowflutter_on": ["关", "开"],
    "active": ["关", "开"],
    "calibration": ["+6", "+3", "0", "-3"],
}

# 每个分区对应的"模块开关"（None = 该区没有可中性化的参数，由顶栏总开关管）
ZONE_MODULES = ["input", None, "transport", "output", "repro_eq"]


@dataclass
class Control:
    name: str
    label: str
    kind: str                 # knob | switch
    rect: QRect
    unit: str = ""
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.01
    options: list = field(default_factory=list)
    module: str = ""          # 所属分区开关名（空 = 不受分区开关影响）

    def value_text(self, value: float) -> str:
        if self.kind == "switch":
            idx = max(0, min(len(self.options) - 1, int(round(value))))
            return self.options[idx]
        txt = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{txt}{self.unit}" if self.unit else txt


class ConsoleSurface(QWidget):
    """面板本体：统一绘制所有控件并做命中测试（和磁带机一样的自绘思路）。"""

    param_changed = Signal(str, float)
    module_toggled = Signal(str, bool)      # 分区开关
    power_toggled = Signal(bool)            # 顶栏总开关
    closed = Signal()

    ZONE_TOGGLE_W = 46

    def __init__(self, skin, values: dict, parent=None):
        super().__init__(parent)
        self.skin = skin
        self.values = dict(values)
        self.controls: list[Control] = []
        self.zone_rects: list[QRect] = []
        self.modules = {}               # 分区开关状态
        self.total_on = True            # 总开关
        self._drag = None            # 拖动中的控件
        self._drag_ref = None        # (起始角度, 起始值)
        self._drag_win_off = None    # 面板空白处拖动整个窗口
        self._hover = None
        self.close_rect = QRect(SURFACE_W - MARGIN - 26, 9, 24, 24)
        self.power_rect = QRect(SURFACE_W - MARGIN - 26 - 84, 12, 76, 22)
        self.setFixedSize(SURFACE_W, SURFACE_H)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self._build()

    # ---------------- 布局 ----------------
    def _build(self):
        self.controls = []
        self.zone_rects = []
        y = TOP_BAR + MARGIN
        for zi, (zone_name, items) in enumerate(ZONES):
            self.zone_rects.append(QRect(MARGIN, y, SURFACE_W - MARGIN * 2, ZONE_H))
            module = ZONE_MODULES[zi] if zi < len(ZONE_MODULES) else None
            cols = len(items)
            col_w = (SURFACE_W - MARGIN * 2) / float(cols)
            knob_r = 26.0 if cols <= 4 else 22.0
            cy = y + 20 + knob_r + 6
            for i, (name, label, kind, unit) in enumerate(items):
                cx = MARGIN + col_w * (i + 0.5)
                size = knob_r * 2
                rect = QRect(int(cx - knob_r), int(cy - knob_r + 12), int(size), int(size))
                lo, hi, step = 0.0, 1.0, 0.01
                if kind == "knob":
                    spec = PARAM_SPEC.get(name)
                    if spec:
                        _lab, lo, hi, step, _is_int, unit = spec
                else:
                    opts = SWITCH_LABELS.get(name) or ENUMS.get(name) or ["关", "开"]
                    lo, hi, step = 0.0, float(len(opts) - 1), 1.0
                    unit = ""
                self.controls.append(Control(name, label, kind, rect, unit, lo, hi, step,
                                             list(self._switch_options(name)),
                                             module or ""))
            y += ZONE_H + ZONE_GAP

    def zone_toggle_rect(self, zi):
        zr = self.zone_rects[zi]
        return QRect(zr.right() - self.ZONE_TOGGLE_W - 8, zr.top() + 3,
                     self.ZONE_TOGGLE_W, 14)

    def set_modules(self, modules: dict):
        self.modules = dict(modules or {})
        self.update()

    def set_power(self, on: bool):
        self.total_on = bool(on)
        self.update()

    @staticmethod
    def _switch_options(name):
        return SWITCH_LABELS.get(name) or ENUMS.get(name) or ["关", "开"]

    def set_skin(self, skin):
        self.skin = skin
        self.update()

    def set_values(self, values: dict):
        self.values.update(values)
        self.update()

    # ---------------- 绘制 ----------------
    def _color(self, key, fallback):
        c = self.skin.color(key) if self.skin is not None else QColor(fallback)
        return c if c.isValid() else QColor(fallback)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.skin
        # 面板底：优先用皮肤资源，否则程序画金属/木纹质感
        if not (s is not None and s.draw_asset(p, "console_panel", QRect(0, 0, SURFACE_W, SURFACE_H))):
            self._paint_panel(p)
        self._paint_top_bar(p)
        y = TOP_BAR + MARGIN
        for zi, (zone_name, items) in enumerate(ZONES):
            self._paint_zone(p, zone_name, y, zi)
            y += ZONE_H + ZONE_GAP
        for c in self.controls:
            off = bool(c.module) and not self.modules.get(c.module, True)
            if off:
                p.setOpacity(0.40)                 # 分区关掉 → 该区控件变暗
            if c.kind == "knob":
                self._paint_knob(p, c)
            else:
                self._paint_switch(p, c)
            if off:
                p.setOpacity(1.0)
        if not self.total_on:                      # 总开关关闭：整面板压暗，一眼看出没在工作
            p.setOpacity(0.42)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 70))
            p.drawRoundedRect(QRectF(MARGIN - 2, TOP_BAR + MARGIN - 2,
                                     SURFACE_W - (MARGIN - 2) * 2, SURFACE_H - TOP_BAR - MARGIN * 2 + 4),
                              11, 11)
            p.setOpacity(1.0)
        p.end()

    def _paint_led_toggle(self, p, r, on, label):
        """小 LED 拨钮：亮绿=开，暗红=关（复古机上的指示灯按键）。"""
        rr = QRectF(r)
        led = QPointF(rr.left() + 6.5, rr.center().y())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawEllipse(led, 4.8, 4.8)
        if on:
            p.setBrush(QColor(170, 255, 195, 70))
            p.drawEllipse(led, 5.8, 5.8)
        p.setBrush(QColor(112, 226, 142) if on else QColor(118, 64, 56))
        p.drawEllipse(led, 3.2, 3.2)
        f = QFont()
        f.setPointSizeF(6.6)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(214, 232, 214) if on else QColor(146, 138, 128))
        p.drawText(QRectF(rr.left() + 13, rr.top(), rr.width() - 13, rr.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, label)

    def _paint_panel(self, p):
        rr = QRectF(6, 6, SURFACE_W - 12, SURFACE_H - 12)
        g = QLinearGradient(0, rr.top(), 0, rr.bottom())
        g.setColorAt(0.0, self._color("console_bg_top", "#3b3a36"))
        g.setColorAt(0.5, self._color("console_bg", "#2e2d2a"))
        g.setColorAt(1.0, self._color("console_bg_bottom", "#232220"))
        p.setPen(QPen(self._color("console_edge", "#15140f"), 2.0))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(rr, 12, 12)
        # 面板边高光
        p.setPen(QPen(QColor(255, 255, 255, 26), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rr.adjusted(3, 3, -3, -3), 10, 10)

    def _paint_top_bar(self, p):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(QRectF(MARGIN, 9, SURFACE_W - MARGIN * 2, 30), 7, 7)
        f = QFont()
        f.setPointSizeF(10.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self._color("console_label", "#e8dcc0"))
        p.drawText(QRectF(MARGIN + 12, 9, SURFACE_W - MARGIN * 2 - 170, 30),
                   Qt.AlignLeft | Qt.AlignVCenter, "D U S K   T A P E   C O N S O L E")
        # 关闭按钮
        cr = self.close_rect
        p.setPen(QPen(QColor(230, 220, 200, 190), 2.2, Qt.SolidLine, Qt.RoundCap))
        d = 6.0
        cx, cy = cr.center().x(), cr.center().y()
        p.drawLine(cx - d, cy - d, cx + d, cy + d)
        p.drawLine(cx + d, cy - d, cx - d, cy + d)
        # 总开关（DSP 级 bypass）
        self._paint_led_toggle(p, self.power_rect, self.total_on, "POWER")

    def _paint_zone(self, p, name, y, zi):
        zr = QRectF(MARGIN, y, SURFACE_W - MARGIN * 2, ZONE_H)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color("console_zone", "#3a3934"))
        p.drawRoundedRect(zr, 9, 9)
        p.setPen(QPen(QColor(0, 0, 0, 90), 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(zr.adjusted(1.2, 1.2, -1.2, -1.2), 8, 8)
        p.setPen(QPen(self._color("console_zone_line", "#55534c"), 1.0))
        p.drawLine(QPointF(zr.left() + 8, zr.top() + 17), QPointF(zr.right() - 8, zr.top() + 17))
        f = QFont()
        f.setPointSizeF(7.6)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self._color("console_label", "#cfc4a8"))
        p.drawText(QRectF(zr.left() + 10, zr.top() + 1, zr.width() - 96, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, name)
        # 该分区的开关（没有可中性化参数的分区标一句"由 POWER 统管"）
        module = ZONE_MODULES[zi] if zi < len(ZONE_MODULES) else None
        if module:
            on = bool(self.modules.get(module, True))
            self._paint_led_toggle(p, self.zone_toggle_rect(zi), on, "ON" if on else "OFF")
        else:
            f2 = QFont()
            f2.setPointSizeF(6.4)
            p.setFont(f2)
            p.setPen(QColor(132, 128, 116))
            p.drawText(QRectF(zr.right() - 64, zr.top() + 2, 56, 14),
                       Qt.AlignRight | Qt.AlignVCenter, "受 POWER 管")

    def _paint_knob(self, p, c: Control):
        r = QRectF(c.rect)
        cx, cy = r.center().x(), r.center().y()
        rad = r.width() / 2.0
        frac = 0.0
        if c.hi > c.lo:
            frac = max(0.0, min(1.0, (self.values.get(c.name, c.lo) - c.lo) / (c.hi - c.lo)))
        span = 270.0
        drag_or_hover = self._drag is c or self._hover is c

        # 刻度环
        for i in range(11):
            t = i / 10.0
            a = math.radians(90.0 - span / 2.0 + span * t)
            ca, sa = math.cos(a), -math.sin(a)
            lit = t <= frac + 1e-6
            p.setPen(QPen(self._color("knob_pointer", "#d8c79a") if lit else
                          QColor(255, 255, 255, 40), 1.6 if lit else 1.2,
                          Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx + ca * (rad - 5.0), cy + sa * (rad - 5.0)),
                       QPointF(cx + ca * (rad - 0.5), cy + sa * (rad - 0.5)))

        cap = rad * 0.72
        # 凹坑
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawEllipse(QPointF(cx, cy + 1.0), cap + 2.2, cap + 2.2)
        # 键帽
        face = self._color("knob_face", "#cfc7b4")
        if drag_or_hover:
            face = face.lighter(112)
        g = QRadialGradient(QPointF(cx - cap * 0.32, cy - cap * 0.36), cap * 1.6)
        g.setColorAt(0.0, face.lighter(128))
        g.setColorAt(0.5, face)
        g.setColorAt(1.0, face.darker(142))
        p.setBrush(QBrush(g))
        p.setPen(QPen(self._color("knob_rim", "#6d6656"), 1.3))
        p.drawEllipse(QPointF(cx, cy), cap, cap)
        # 滚花
        p.setPen(QPen(QColor(0, 0, 0, 70), 1.0))
        for i in range(20):
            a = math.radians(i * 18.0)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(cx + ca * cap * 0.87, cy + sa * cap * 0.87),
                       QPointF(cx + ca * cap * 0.98, cy + sa * cap * 0.98))
        # 指针
        a = math.radians(90.0 - span / 2.0 + span * frac)
        ca, sa = math.cos(a), -math.sin(a)
        p.setPen(QPen(self._color("knob_mark", "#241d14"), 2.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx + ca * cap * 0.12, cy + sa * cap * 0.12),
                   QPointF(cx + ca * cap * 0.86, cy + sa * cap * 0.86))

        # 标签与数值
        f = QFont()
        f.setPointSizeF(7.0)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self._color("console_label", "#cfc4a8"))
        p.drawText(QRectF(r.left() - 14, r.bottom() - 2, r.width() + 28, 13),
                   Qt.AlignHCenter | Qt.AlignVCenter, c.label)
        f2 = QFont("Courier New")
        f2.setPointSizeF(7.0)
        f2.setBold(True)
        p.setFont(f2)
        p.setPen(QColor(255, 226, 150) if drag_or_hover else QColor(150, 148, 132))
        p.drawText(QRectF(r.left() - 20, r.bottom() + 10, r.width() + 40, 13),
                   Qt.AlignHCenter | Qt.AlignVCenter, c.value_text(self.values.get(c.name, c.lo)))

    def _paint_switch(self, p, c: Control):
        r = QRectF(c.rect)
        cx, cy = r.center().x(), r.center().y()
        rad = r.width() / 2.0
        n = max(2, len(c.options))
        idx = max(0, min(n - 1, int(round(self.values.get(c.name, 0.0)))))
        hover = self._drag is c or self._hover is c

        # 档位刻度点（沿上半圈均匀）
        span = min(240.0, 40.0 * (n - 1))
        for i in range(n):
            t = i / (n - 1.0) if n > 1 else 0.5
            a = math.radians(90.0 - span / 2.0 + span * t)
            ca, sa = math.cos(a), -math.sin(a)
            on = i == idx
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 214, 120) if on else QColor(255, 255, 255, 55))
            p.drawEllipse(QPointF(cx + ca * (rad - 3.0), cy + sa * (rad - 3.0)), 2.0, 2.0)

        cap = rad * 0.68
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        p.drawEllipse(QPointF(cx, cy + 1.0), cap + 2.2, cap + 2.2)
        face = self._color("switch_face", "#5d5a52")
        if hover:
            face = face.lighter(115)
        g = QRadialGradient(QPointF(cx - cap * 0.3, cy - cap * 0.35), cap * 1.6)
        g.setColorAt(0.0, face.lighter(130))
        g.setColorAt(1.0, face.darker(150))
        p.setBrush(QBrush(g))
        p.setPen(QPen(self._color("knob_rim", "#6d6656"), 1.3))
        p.drawEllipse(QPointF(cx, cy), cap, cap)

        # 拨杆
        t = idx / (n - 1.0) if n > 1 else 0.5
        a = math.radians(90.0 - span / 2.0 + span * t)
        ca, sa = math.cos(a), -math.sin(a)
        p.setPen(QPen(QColor("#e9e2cd"), 2.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, cy), QPointF(cx + ca * cap * 0.86, cy + sa * cap * 0.86))

        f = QFont()
        f.setPointSizeF(7.0)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self._color("console_label", "#cfc4a8"))
        p.drawText(QRectF(r.left() - 14, r.bottom() - 2, r.width() + 28, 13),
                   Qt.AlignHCenter | Qt.AlignVCenter, c.label)
        f2 = QFont()
        f2.setPointSizeF(7.0)
        f2.setBold(True)
        p.setFont(f2)
        p.setPen(QColor(120, 220, 150) if c.name == "active" and idx == 1 else
                 (QColor(255, 226, 150) if hover else QColor(150, 148, 132)))
        p.drawText(QRectF(r.left() - 26, r.bottom() + 10, r.width() + 52, 13),
                   Qt.AlignHCenter | Qt.AlignVCenter, c.value_text(self.values.get(c.name, 0.0)))

    # ---------------- 交互 ----------------
    def _hit(self, pos):
        for c in self.controls:
            if c.rect.adjusted(-10, -10, 10, 22).contains(pos.toPoint()):
                return c
        return None

    @staticmethod
    def _angle(pos, c: Control):
        centre = c.rect.center()
        return math.degrees(math.atan2(pos.x() - centre.x(), -(pos.y() - centre.y())))

    def mousePressEvent(self, e):
        pos = e.position()
        if self.close_rect.contains(pos.toPoint()):
            self.closed.emit()
            return
        if self.power_rect.contains(pos.toPoint()):          # 顶栏总开关
            self.total_on = not self.total_on
            self.power_toggled.emit(self.total_on)
            self.update()
            return
        for zi in range(len(self.zone_rects)):               # 分区开关
            module = ZONE_MODULES[zi] if zi < len(ZONE_MODULES) else None
            if module and self.zone_toggle_rect(zi).contains(pos.toPoint()):
                on = not bool(self.modules.get(module, True))
                self.modules[module] = on
                self.module_toggled.emit(module, on)
                self.update()
                return
        c = self._hit(pos)
        if c is None:
            # 面板空白处：拖动整个窗口。注意 surface 铺满窗口，事件不会自动冒泡给
            # ConsoleWindow，所以拖动必须在这里自己实现（否则只有投影边距能拖）。
            self._drag_win_off = e.globalPosition().toPoint() - self.window().pos()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if c.kind == "knob":
            if e.button() == Qt.LeftButton:
                self._drag, self._drag_ref = c, (self._angle(pos, c), self.values.get(c.name, c.lo))
        elif e.button() == Qt.LeftButton:
            self._bump_switch(c, +1)
        self.update()

    def _bump_switch(self, c: Control, direction: int):
        n = len(c.options)
        cur = int(round(self.values.get(c.name, 0.0)))
        nxt = max(0, min(n - 1, cur + direction))
        if nxt != cur:
            self.values[c.name] = float(nxt)
            self.param_changed.emit(c.name, float(nxt))

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._drag_win_off is not None:                    # 拖窗口优先
            self.window().move(e.globalPosition().toPoint() - self._drag_win_off)
            return
        c = self._hit(pos)
        if c is not self._hover:
            self._hover = c
            if c is None:
                self.setCursor(Qt.OpenHandCursor)             # 空白处提示可拖动
            else:
                self.setCursor(Qt.PointingHandCursor)
            self.update()
        if self._drag is not None and self._drag_ref is not None:
            ref_ang, ref_val = self._drag_ref
            delta = self._angle(pos, self._drag) - ref_ang
            delta = (delta + 180.0) % 360.0 - 180.0
            span = 270.0
            c = self._drag
            v = ref_val + (delta / span) * (c.hi - c.lo)
            v = max(c.lo, min(c.hi, v))
            if c.step > 0:
                v = round(v / c.step) * c.step
            if abs(v - self.values.get(c.name, c.lo)) > 1e-9:
                self.values[c.name] = float(v)
                self.param_changed.emit(c.name, float(v))
                self.update()

    def mouseReleaseEvent(self, e):
        if self._drag_win_off is not None:
            self._drag_win_off = None
            self.setCursor(Qt.OpenHandCursor if self._hit(e.position()) is None
                           else Qt.PointingHandCursor)
            return
        if self._drag is not None:
            self._drag, self._drag_ref = None, None
            self.update()

    def wheelEvent(self, e):
        c = self._hit(e.position())
        if c is None:
            return
        steps = e.angleDelta().y() / 120.0
        if abs(steps) < 1e-6:
            return
        if c.kind == "switch":
            self._bump_switch(c, +1 if steps > 0 else -1)
        else:
            v = self.values.get(c.name, c.lo) + steps * c.step * 2.0
            v = max(c.lo, min(c.hi, v))
            self.values[c.name] = float(v)
            self.param_changed.emit(c.name, float(v))
        self.update()
        e.accept()


class ConsoleWindow(ShadowWindow):
    """独立悬浮的磁带 console 面板。"""

    param_changed = Signal(str, float)
    module_toggled = Signal(str, bool)
    power_toggled = Signal(bool)

    def __init__(self, skin, values: dict, parent=None):
        super().__init__(margin=18, blur=28)
        self.setWindowTitle("磁带 console")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.surface = ConsoleSurface(skin, values)
        self.add_content(self.surface)
        self.surface.param_changed.connect(self.param_changed)
        self.surface.module_toggled.connect(self.module_toggled)
        self.surface.power_toggled.connect(self.power_toggled)
        self.surface.closed.connect(self.hide)
        self._drag_off = None

    def sync_state(self, values: dict, modules: dict, power_on: bool):
        """把引擎侧的真实状态同步到面板（打开面板时调用）。"""
        self.surface.set_values(values)
        self.surface.set_modules(modules)
        self.surface.set_power(power_on)

    def set_skin(self, skin):
        self.surface.set_skin(skin)
        self.rebuild_shadow()

    def set_values(self, values: dict):
        self.surface.set_values(values)

    # 无标题栏：面板空白处按住即可拖动
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, e):
        self._drag_off = None
