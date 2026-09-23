"""磁带机主窗口：QPainter 全矢量绘制的底座 + 磁带 + 按钮，支持皮肤包覆盖。

布局（默认皮肤几何）：
- 磁带区 (220,96,520,356)：封面居中偏上、标题/副标题在封面下方、两滚轴在下半部、LCD 计数器居中。
- 底座梯形 (76,468,808,132)：控制按钮组（上一首/播放暂停/下一首/停止/循环）+ 窗口小按钮组（最小化/菜单/列表/关闭）。
"""
import math

from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QRect, QRectF, QPointF, Signal
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QPixmap, QFont, QPainterPath,
                           QLinearGradient, QRadialGradient, QPolygonF)
from PySide6.QtWidgets import QWidget

from .skin_loader import Skin
from .tape_anim import TapeAnimator, clamp01


LOOP_LABELS = ["顺序播放", "单曲循环", "列表循环", "随机播放"]


class DeckWidget(QWidget):
    action = Signal(str)             # prev / next / toggle_play / stop / loop / min / list / close
    menu_requested = Signal(QPointF)  # 菜单按钮的全局坐标
    seek_requested = Signal(float)    # 观察窗进度条松手：请求跳到该秒数
    volume_changed = Signal(float)    # 面板音量旋钮：0..1 线性音量

    CTRL_KEYS = ("btn_prev", "btn_play", "btn_next", "btn_stop", "btn_loop", "btn_console")
    CHROME_KEYS = ("win_min", "win_menu", "win_list", "win_close")

    # ---------------- 磁带内部几何（相对磁带中心，单位 px，对应 layout.tape 520x356） ----------------
    COVER_SIZE = 118
    COVER_CY = -88
    LABEL_RECT = QRectF(-234, -152, 468, 190)      # 白色标签纸
    WINDOW_RECT = QRectF(-156, 44, 312, 96)        # 中间观察窗（能看到两个齿轮盘）
    HUB_X = 112                                    # 齿轮盘中心 x（±），约等于实物磁带轮距比例
    HUB_Y = 92
    STRIP_RECT = QRectF(-234, 146, 468, 24)        # 底部标签条（型号字样 + 计数器）
    LCD_RECT = QRectF(-66, 148, 132, 20)
    SLOT_RECT = QRectF(-96, 172, 192, 6)           # 底部定位槽
    HUB_R = 26                                     # 齿轮盘固定半径（实物磁带盘径不变，只有带卷变）
    PROG_RECT = QRectF(-64, 121, 128, 8)           # 走带进度条（两转轴之间，可拖动）
    PROG_HIT = QRectF(-74, 111, 148, 28)           # 进度条命中区（比轨道大一圈，好点中）
    KNOB_SPAN = 300.0                              # 音量旋钮可转范围（度）：-150°=静音，+150°=最大
    KNOB_FALLBACK = QRect(134, 508, 64, 64)        # 皮肤未提供 layout.knob 时的旋钮区域
    # 主界面 VU 表：贴在磁带机两侧机身上，指针由磁带 DSP 的实际电平驱动
    VU_LEFT_FALLBACK = QRect(46, 296, 148, 96)
    VU_RIGHT_FALLBACK = QRect(766, 296, 148, 96)
    VU_DB_MIN = -30.0                              # 表盘左端（-30dB：普通音乐的平均电平也能落在中段）
    VU_DB_MAX = 3.0                                # 表盘右端（红区起点为 0 dB）
    VU_ARC = 46.0                                  # 指针相对中线左右各摆动多少度
    VU_OVER_DB = 0.0                               # 过载灯点亮的阈值（dB）
    KEY2ACTION = {"btn_prev": "prev", "btn_play": "toggle_play", "btn_next": "next",
                  "btn_stop": "stop", "btn_loop": "loop", "btn_console": "console",
                  "win_min": "min", "win_menu": None, "win_list": "list", "win_close": "close"}

    def __init__(self, skin: Skin):
        super().__init__()
        self.skin = skin
        self.setFixedSize(960, 620)
        # 本部件自己负责"只画底座/磁带/按钮"：声明无系统背景，别让 Qt 填一层不透明底色
        # （顶层窗口本来就是 WA_TranslucentBackground，这条也让离屏截图/抓图得到真实透明像素）
        self.setAttribute(Qt.WA_NoSystemBackground)
        self._anim = TapeAnimator()
        self.cover_pixmap = None
        self.title_text = "未插入磁带"
        self.sub_text = "从菜单打开文件夹或音频文件"
        self.playing = False
        self.loop_mode = 0
        self.volume = 0.8               # 面板音量旋钮（0..1），由 app 层与引擎同步
        self._knob_ref_ang = None       # 拖动旋钮的起始角度（增量式，指针不会突跳）
        self._knob_hover = False
        # VU 表：target 来自 DSP 表头读数，disp 是指针当前显示的归一化位置（带惯性）
        self._vu_target = [0.0, 0.0]
        self._vu_disp = [0.0, 0.0]
        self._vu_hot = [0.0, 0.0]       # 过载灯余辉（秒）
        self._vu_peak = [0.0, 0.0]
        self.pos_now = 0.0
        self.dur_total = 0.0
        self._hover_key = None
        self._pressed_key = None        # 按下中的按钮（塑料键按下的下沉反馈）
        self._drag_off = None           # 无标题栏：按住非按钮区域拖动整个窗口
        self._drag_frac = None          # 拖动进度条时的预览比例（None = 未拖动）
        self._drag_pos = None           # 拖动进度条时的预览秒数
        self._prog_hover = False
        self._shown_sec = -1            # 上一次绘制的整秒，用于按需重绘计数器
        self._hit_map: list[tuple[QRect, str]] = []
        self._build_hit_map()
        # 静态层缓存：底座 + 按钮 + 标签在状态不变时不必每帧重画（动画帧只画磁带）
        self._layer_pm = None
        self._layer_key = None
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)      # 必须持引用：局部变量会被 GC，定时器随之失效
        self._timer.setTimerType(Qt.PreciseTimer)     # 换带动画要的是稳定节拍，不是省电
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---------------- 状态设置（app 层调用） ----------------
    def set_skin(self, skin: Skin):
        self.skin = skin
        self._layer_pm = None           # 换皮肤后静态层必须重建
        self._layer_key = None
        self._build_hit_map()
        self.update()

    def set_labels(self, title, sub):
        self.title_text = title or ""
        self.sub_text = sub or ""
        self.update(self._tape_rect())

    def set_cover(self, pm):
        self.cover_pixmap = pm      # None → 占位封面
        self.update(self._tape_rect())

    def set_progress(self, pos, dur):
        self.pos_now = pos
        self.dur_total = dur if dur and dur > 0 else 0.0
        self._anim.progress = min(1.0, pos / dur) if dur and dur > 0 else 0.0
        sec = int(pos)
        if sec != self._shown_sec:        # 秒数变了才重绘，播放时每秒约 7 次刷新计数器
            self._shown_sec = sec
            self.update(self._tape_rect())

    def set_playing(self, b):
        self.playing = bool(b)

    def set_loop_mode(self, m):
        self.loop_mode = max(0, min(3, int(m)))      # 0=顺序 1=单曲 2=列表 3=随机
        self.update()

    def seek_burst(self, direction, magnitude=3.5):
        self._anim.seek_burst(direction, magnitude)

    # ---------------- 音量旋钮 ----------------
    def _knob_rect(self):
        r = self.skin.rect("knob")
        return r if r is not None else self.KNOB_FALLBACK

    def _knob_update_rect(self):
        r = self._knob_rect()
        return r.adjusted(-12, -12, 12, 18)      # 含刻度环与下方的 VOL 文字

    def _knob_hit(self, pos):
        r = self._knob_rect()
        c = QPointF(r.center())
        d = QPointF(pos) - c
        return math.hypot(d.x(), d.y()) <= r.width() / 2.0 + 5.0

    def _knob_angle(self, pos):
        """鼠标相对旋钮中心的角度：0° 为正上方，顺时针为正。"""
        c = QPointF(self._knob_rect().center())
        d = QPointF(pos) - c
        return math.degrees(math.atan2(d.x(), -d.y()))

    def set_volume(self, v, notify=True):
        v = clamp01(float(v))
        if abs(v - self.volume) < 1e-4:
            return
        self.volume = v
        self.update(self._knob_update_rect())
        if notify:
            self.volume_changed.emit(v)

    # ---------------- VU 表 ----------------
    def _vu_rect(self, side):
        key = "vu_left" if side == 0 else "vu_right"
        r = self.skin.rect(key)
        if r is not None:
            return r
        return self.VU_LEFT_FALLBACK if side == 0 else self.VU_RIGHT_FALLBACK

    def _vu_update_rect(self):
        r = self._vu_rect(0).united(self._vu_rect(1))
        return r.adjusted(-10, -10, 10, 10)

    @classmethod
    def _vu_norm(cls, value):
        """线性幅度 → 表盘归一化位置（0=最左 -20dB，1=最右 +3dB）。"""
        v = max(float(value or 0.0), 1e-5)
        db = 20.0 * math.log10(v)
        return max(0.0, min(1.0, (db - cls.VU_DB_MIN) / (cls.VU_DB_MAX - cls.VU_DB_MIN)))

    def _vu_angle_deg(self, t):
        """归一化位置 t → 弧上角度（Qt 逆时针为正）：t=0 在左、t=1 在右。"""
        return 90.0 - self.VU_ARC * (2.0 * float(t) - 1.0)

    def _vu_dir(self, t):
        """归一化位置 → 屏幕方向向量（y 向下为正，所以 sin 取负）。"""
        a = math.radians(self._vu_angle_deg(t))
        return math.cos(a), -math.sin(a)

    def set_meters(self, meters):
        """由 app 层按定时器喂入 DSP 表头读数（{'vu_l','vu_r','out_peak_l',...}）。"""
        for i, key in enumerate(("vu_l", "vu_r")):
            self._vu_target[i] = self._vu_norm(meters.get(key, 0.0))
            peak_db = 20.0 * math.log10(max(float(meters.get(
                "out_peak_l" if i == 0 else "out_peak_r", 0.0) or 0.0), 1e-5))
            if peak_db >= self.VU_OVER_DB:
                self._vu_hot[i] = 1.2                       # 过载灯余辉
            self._vu_peak[i] = max(self._vu_target[i], self._vu_peak[i] * 0.995)

    def _update_vu(self, dt):
        """指针惯性：起针快、回落慢，像真的动圈表。返回是否有指针在动。"""
        moving = False
        for i in (0, 1):
            cur, tgt = self._vu_disp[i], self._vu_target[i]
            k = 14.0 if tgt > cur else 4.5
            nxt = cur + (tgt - cur) * min(1.0, k * dt)
            if abs(nxt - cur) > 2e-4:
                moving = True
            self._vu_disp[i] = nxt
            if self._vu_hot[i] > 0.0:
                self._vu_hot[i] = max(0.0, self._vu_hot[i] - dt)
                moving = True
        return moving

    def _paint_vu(self, p):
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        for side, label in ((0, "L"), (1, "R")):
            self._paint_vu_meter(p, self._vu_rect(side), self._vu_disp[side],
                                 self._vu_hot[side], label)
        p.restore()

    def _paint_vu_meter(self, p, r, pos, hot, label):
        """一只复古动圈 VU 表：木质外框 + 奶油表盘 + 刻度弧 + 指针 + 过载灯。"""
        rr = QRectF(r)
        cx = rr.center().x()
        dial = rr.adjusted(5.0, 5.0, -5.0, -5.0)
        axis = QPointF(cx, dial.top() + dial.height() * 0.9)     # 指针转轴（表盘偏下）
        rad_arc = dial.height() * 0.78

        # 外框（深色木质感）
        frame = QLinearGradient(0, rr.top(), 0, rr.bottom())
        frame.setColorAt(0.0, QColor("#5d4a35"))
        frame.setColorAt(0.5, QColor("#3f3121"))
        frame.setColorAt(1.0, QColor("#2a1f14"))
        p.setPen(QPen(QColor("#1c1509"), 1.6))
        p.setBrush(QBrush(frame))
        p.drawRoundedRect(rr, 7, 7)

        # 玻璃罩：上半亮、下半暗
        glass = QLinearGradient(0, dial.top(), 0, dial.bottom())
        glass.setColorAt(0.0, QColor("#fdf8e8"))
        glass.setColorAt(0.55, QColor("#f3ead2"))
        glass.setColorAt(1.0, QColor("#ddd0b0"))
        p.setPen(QPen(QColor("#8a7a5c"), 1.0))
        p.setBrush(QBrush(glass))
        p.drawRoundedRect(dial, 5, 5)

        arc_rect = QRectF(axis.x() - rad_arc, axis.y() - rad_arc, rad_arc * 2, rad_arc * 2)
        t_red = (self.VU_OVER_DB - self.VU_DB_MIN) / (self.VU_DB_MAX - self.VU_DB_MIN)

        # 过载红弧（0 dB → +3 dB 那一段）
        p.setPen(QPen(QColor("#cf4a3c"), 3.6, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(arc_rect, int(self._vu_angle_deg(t_red) * 16),
                  int((self._vu_angle_deg(1.0) - self._vu_angle_deg(t_red)) * 16))

        # 刻度（21 道，每 5 道一条长刻；红区内刻度用红色）
        for i in range(21):
            t = i / 20.0
            ca, sa = self._vu_dir(t)
            major = i % 5 == 0
            inner = rad_arc - (9.0 if major else 5.0)
            p.setPen(QPen(QColor("#a33628") if t >= t_red else QColor("#4a4033"),
                          2.0 if major else 1.1, Qt.SolidLine, Qt.FlatCap))
            p.drawLine(QPointF(axis.x() + ca * inner, axis.y() + sa * inner),
                       QPointF(axis.x() + ca * rad_arc, axis.y() + sa * rad_arc))

        # 刻度数字（画在刻度内侧，沿线排布）
        f = QFont()
        f.setPointSizeF(6.0)
        f.setBold(True)
        p.setFont(f)
        for db in (-30, -20, -10, 0, 3):
            t = (db - self.VU_DB_MIN) / (self.VU_DB_MAX - self.VU_DB_MIN)
            ca, sa = self._vu_dir(t)
            tx = axis.x() + ca * (rad_arc - 17.0)
            ty = axis.y() + sa * (rad_arc - 17.0)
            p.setPen(QColor("#a33628") if db >= 0 else QColor("#5a5040"))
            p.drawText(QRectF(tx - 11, ty - 6, 22, 12), Qt.AlignCenter,
                       f"+{db}" if db > 0 else f"{db}")

        # 指针（含轴帽）
        ca, sa = self._vu_dir(pos)
        tip = QPointF(axis.x() + ca * (rad_arc - 2.5), axis.y() + sa * (rad_arc - 2.5))
        p.setPen(QPen(QColor("#241d14"), 1.9, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(axis.x() - ca * 6.0, axis.y() - sa * 6.0), tip)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#241d14"))
        p.drawEllipse(axis, 3.4, 3.4)

        # 玻璃反光
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 46))
        p.drawPolygon(QPolygonF([
            QPointF(dial.left() + 2, dial.top() + 2),
            QPointF(dial.left() + dial.width() * 0.45, dial.top() + 2),
            QPointF(dial.left() + dial.width() * 0.18, dial.bottom() - 2),
            QPointF(dial.left() + 2, dial.bottom() - 2),
        ]))

        # 过载灯 + 声道标记
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 96, 64) if hot > 0 else QColor(96, 62, 52))
        p.drawEllipse(QPointF(dial.right() - 12, dial.bottom() - 11), 3.2, 3.2)
        f2 = QFont()
        f2.setPointSizeF(7.0)
        f2.setBold(True)
        p.setFont(f2)
        p.setPen(QColor("#4a4033"))
        p.drawText(QRectF(dial.left() + 6, dial.bottom() - 18, 30, 14), Qt.AlignLeft | Qt.AlignVCenter, label)

    def trigger_swap(self):
        self._anim.trigger_swap()

    # ---------------- 内部 ----------------
    def _build_hit_map(self):
        self._hit_map = []
        for k in (*self.CTRL_KEYS, *self.CHROME_KEYS):
            r = self.skin.rect(k)
            if r is not None:
                self._hit_map.append((r, k))

    def _tick(self):
        dt = min(0.05, max(0.0, self._clock.restart() / 1000.0))   # 用真实经过时间驱动，帧率波动不影响动画时长
        a = self._anim
        vu_moving = self._update_vu(dt)
        if not (self.playing or a.swapping or a.burst_v > 0.01 or vu_moving):
            return
        a.update(dt, self.playing)
        rect = self._tape_rect()
        if vu_moving:
            rect = rect.united(self._vu_update_rect())
        self.update(rect)

    def _tape_rect(self):
        """磁带动画影响的区域（含拔出方向的上方余量），用于局部重绘。"""
        r = self.skin.rect("tape")
        if r is None:
            return self.rect()
        return r.adjusted(-24, -56, 24, 24)

    # ---------------- 绘制 ----------------
    def _static_layer(self):
        """底座 + 按钮 + 模式标签的合成位图；状态没变就直接复用。"""
        key = (id(self.skin), self._hover_key, self._pressed_key, self.playing, self.loop_mode)
        if self._layer_pm is not None and key == self._layer_key:
            return self._layer_pm
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        self._paint_base(p)
        for k in self.CTRL_KEYS:
            self._paint_button(p, k)
        for k in self.CHROME_KEYS:
            self._paint_button(p, k)
        self._paint_loop_label(p)
        p.end()
        self._layer_pm, self._layer_key = pm, key
        return pm

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)   # 换带时磁带会旋转，位图别出锯齿
        # 背景留空：窗口是透明的，只有底座/磁带/按钮被绘制出来
        p.drawPixmap(0, 0, self._static_layer())
        self._paint_vu(p)            # 两侧 VU 表（指针带惯性，独立实时绘制）
        self._paint_knob(p)          # 音量旋钮实时画（转一下只重绘旋钮那一小块）
        self._paint_tape(p)
        p.end()

    def _paint_base(self, p):
        s = self.skin
        r = s.rect("base")
        if r is None:
            return

        def fb(pp):
            x, y, wdt, hgt = r.x(), r.y(), r.width(), r.height()
            inset = 74
            path = QPainterPath()
            path.moveTo(x + inset, y)
            path.lineTo(x + wdt - inset, y)
            path.lineTo(x + wdt, y + hgt)
            path.lineTo(x, y + hgt)
            path.closeSubpath()
            pp.setPen(QPen(s.color("base_edge"), 3))
            pp.setBrush(s.color("base"))
            pp.drawPath(path)

            # 磁带槽腔（顶部中央，比磁带宽一点）
            tape_r = s.rect("tape")
            if tape_r is not None:
                sw = tape_r.width() + 16
                pp.setPen(Qt.NoPen)
                pp.setBrush(s.color("base_edge").darker(160))
                pp.drawRoundedRect(QRectF(x + (wdt - sw) / 2.0, y, sw, 22), 7, 7)

            # 控制按钮 / 窗口按钮的下沉面板凹槽
            for keys, pad in ((("btn_prev", "btn_loop"), (7, 6)), (("win_min", "win_close"), (9, 7))):
                r0, r1 = s.rect(keys[0]), s.rect(keys[1])
                if r0 is None or r1 is None:
                    continue
                prr = QRectF(r0.left() - pad[0], r0.top() - pad[1],
                             r1.right() - r0.left() + pad[0] * 2, r0.height() + pad[1] * 2)
                pp.setPen(Qt.NoPen)
                pp.setBrush(s.color("base_edge").darker(130))
                pp.drawRoundedRect(prr, 12, 12)
                pp.setPen(QPen(s.color("base_lip"), 1.0))
                pp.setBrush(Qt.NoBrush)
                pp.drawRoundedRect(prr.adjusted(2.0, 2.0, -2.0, -2.0), 10.5, 10.5)

            # 品牌刻字
            f = QFont()
            f.setPointSizeF(8.0)
            f.setBold(True)
            pp.setFont(f)
            pp.setPen(s.color("base_lip"))
            pp.drawText(QRectF(x + inset, y + 30, wdt - 2 * inset, 15),
                        Qt.AlignHCenter | Qt.AlignVCenter, "R E T R O   D E C K")

            # 顶部高光唇边
            lip = s.color("base_lip")
            pp.setPen(QPen(lip, 2.5))
            pp.drawLine(x + inset + 6, y + 7, x + wdt - inset - 6, y + 7)
            # 底部阴影条
            pp.setPen(Qt.NoPen)
            pp.setBrush(s.color("base_edge"))
            p2 = QPainterPath()
            p2.addRoundedRect(x + 34, y + hgt - 15, wdt - 68, 7, 3.5, 3.5)
            pp.save()
            pp.setOpacity(0.45)
            pp.drawPath(p2)
            pp.restore()

            # 扬声器网孔（右端；左端留给音量旋钮）
            pp.setPen(Qt.NoPen)
            pp.setBrush(s.color("base_edge").lighter(115))
            for row in range(3):
                for col in range(7):
                    pp.drawEllipse(QPointF(x + wdt - 184 + col * 14, y + 62 + row * 16), 2.5, 2.5)

        s.draw_asset(p, "base", r, fb)

    def _paint_button(self, p, key):
        s = self.skin
        r = s.rect(key)
        if r is None:
            return
        if key == "btn_play":
            asset_key = "btn_pause" if self.playing else "btn_play"
        elif key == "btn_loop":
            # 四种播放模式各用一张图：顺序 / 单曲 / 列表 / 随机
            asset_key = ("btn_sequence", "btn_repeat_one", "btn_loop", "btn_shuffle")[self.loop_mode]
        else:
            asset_key = key
        hover = self._hover_key == key
        pressed = self._pressed_key == key
        glyph_c = s.color("chrome_glyph" if key in self.CHROME_KEYS else "btn_glyph")

        def fb(pp):
            self._paint_key_body(pp, QRectF(r), key, hover, pressed)
            pp.save()
            if pressed:
                pp.translate(0.0, 1.4)          # 按下时键帽与图标一起下沉
            self._glyph(pp, asset_key, QRectF(r), glyph_c)
            pp.restore()

        # 状态变体：给了 btn_play_down / btn_play_hover 就用皮肤自己的图，
        # 没给就用 base_paint（程序按 hover/pressed 调制颜色）——旧皮肤行为不变。
        state = "down" if pressed else ("hover" if hover else None)
        s.draw_part(p, asset_key, r, state=state, fallback=fb)

    def _paint_key_body(self, p, r, key, hover, pressed):
        """复古塑料键帽：外壳上挖出的键槽 + 上亮下暗的塑料面 + 倒角高光。"""
        s = self.skin
        chrome = key in self.CHROME_KEYS
        radius = 7.0 if chrome else 9.5
        rr = QRectF(r).adjusted(1.7, 1.7, -1.7, -1.7)
        if chrome:
            face = s.color("chrome_hover") if hover else s.color("chrome_bg")
        else:
            face = s.color("btn_hover") if hover else s.color("btn_bg")
        if pressed:
            face = face.darker(114)

        # 键槽（凹口：上沿暗、下沿留出亮边，做出"嵌进面板"的深度）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 95))
        p.drawRoundedRect(rr.adjusted(-1.7, -0.5, 1.7, 1.9), radius + 2.4, radius + 2.4)

        # 键帽本体
        grad = QLinearGradient(0, rr.top(), 0, rr.bottom())
        grad.setColorAt(0.0, face.lighter(134))
        grad.setColorAt(0.45, face)
        grad.setColorAt(1.0, face.darker(126))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(face.darker(180), 1.5))
        p.drawRoundedRect(rr, radius, radius)

        # 塑料倒角：顶部内高光 / 底部内阴影
        p.setPen(QPen(QColor(255, 255, 255, 132), 1.4))
        p.drawLine(QPointF(rr.left() + radius * 0.85, rr.top() + 2.4),
                   QPointF(rr.right() - radius * 0.85, rr.top() + 2.4))
        p.setPen(QPen(QColor(0, 0, 0, 76), 1.4))
        p.drawLine(QPointF(rr.left() + radius * 0.85, rr.bottom() - 2.4),
                   QPointF(rr.right() - radius * 0.85, rr.bottom() - 2.4))
        # 键面内圈刻线
        p.setPen(QPen(face.darker(142), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rr.adjusted(3.4, 3.4, -3.4, -3.4), radius * 0.6, radius * 0.6)

    def _paint_loop_label(self, p):
        r = self.skin.rect("btn_loop")
        if r is None:
            return
        f = QFont()
        f.setPointSizeF(9.5)
        f.setBold(True)
        p.setFont(f)
        p.setPen(self.skin.color("text_sub"))
        # 画在循环按钮下方（按钮内会被皮肤图标占据，标签另起一行更清楚）
        label_rect = QRect(r.x() - 40, r.y() + r.height() + 2, r.width() + 80, 18)
        p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignVCenter, LOOP_LABELS[self.loop_mode])

    def _paint_knob(self, p):
        """底座上的复古音量旋钮：刻度环 + 滚花旋钮帽 + 指针 + VOL 读数。

        指针角度 = 音量，可转范围 KNOB_SPAN 度（左下静音、右下最大），
        已过的刻度会亮起，跟老式卡座上的音量钮一样。
        """
        s = self.skin
        r = self._knob_rect()
        cx, cy = float(r.center().x()), float(r.center().y())
        ring = r.width() / 2.0
        cap = ring * 0.72
        span = self.KNOB_SPAN

        p.save()
        p.setRenderHint(QPainter.Antialiasing)

        # 刻度环（11 道，已过音量的亮起）
        for i in range(11):
            frac = i / 10.0
            ang = math.radians(-span / 2 + span * frac)
            sx, sy = math.sin(ang), -math.cos(ang)
            lit = frac <= self.volume + 1e-6
            p.setPen(QPen(s.color("base_lip") if lit else s.color("base_edge").lighter(125),
                          2.2 if lit else 1.6, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx + sx * (ring - 7.0), cy + sy * (ring - 7.0)),
                       QPointF(cx + sx * (ring - 1.5), cy + sy * (ring - 1.5)))

        # 旋钮凹坑（嵌在面板里的那一圈阴影）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 96))
        p.drawEllipse(QPointF(cx, cy + 1.2), cap + 2.6, cap + 2.6)

        # 旋钮帽（左上受光的径向渐变）
        grad = QRadialGradient(QPointF(cx - cap * 0.32, cy - cap * 0.38), cap * 1.6)
        grad.setColorAt(0.0, QColor("#f4f8fa"))
        grad.setColorAt(0.42, QColor("#ccd7de"))
        grad.setColorAt(1.0, QColor("#8a98a2"))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#46565f"), 1.6))
        p.drawEllipse(QPointF(cx, cy), cap, cap)

        # 滚花齿纹
        p.setPen(QPen(QColor(66, 78, 88, 150), 1.2))
        for i in range(28):
            ang = math.radians(i * 360.0 / 28.0)
            ca, sa = math.cos(ang), math.sin(ang)
            p.drawLine(QPointF(cx + ca * cap * 0.85, cy + sa * cap * 0.85),
                       QPointF(cx + ca * cap * 0.99, cy + sa * cap * 0.99))

        # 内圈亮线
        p.setPen(QPen(QColor(255, 255, 255, 118), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), cap * 0.6, cap * 0.6)

        # 指针 + 中心轴
        ang = math.radians(-span / 2 + span * self.volume)
        sx, sy = math.sin(ang), -math.cos(ang)
        p.setPen(QPen(QColor("#1e2c36"), 2.8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx + sx * cap * 0.16, cy + sy * cap * 0.16),
                   QPointF(cx + sx * cap * 0.8, cy + sy * cap * 0.8))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#5a6b76"))
        p.drawEllipse(QPointF(cx, cy), cap * 0.15, cap * 0.15)

        # VOL 读数（悬停或拧动时更亮，方便看数值）
        f = QFont()
        f.setPointSizeF(7.5)
        f.setBold(True)
        p.setFont(f)
        active = self._knob_ref_ang is not None or self._knob_hover
        p.setPen(QColor(255, 255, 255) if active else s.color("base_lip"))
        p.drawText(QRectF(r.left() - 14, cy + ring + 1, r.width() + 28, 14),
                   Qt.AlignHCenter | Qt.AlignVCenter, f"VOL {int(round(self.volume * 100))}")
        p.restore()

    def _paint_tape(self, p):
        s = self.skin
        a = self._anim
        r = s.rect("tape")
        if r is None:
            return
        p.save()
        p.translate(r.center().x(), r.center().y() + a.offset_y)
        if abs(a.rot_deg) > 0.01:
            p.rotate(a.rot_deg)
        p.setOpacity(max(0.0, min(1.0, a.alpha)))

        hw, hh = r.width() / 2.0, r.height() / 2.0
        shell_rect = QRect(-int(hw), -int(hh), int(r.width()), int(r.height()))

        # ① 插进底座槽位处的投影（最底层，制造"插到底座上"的观感）
        self._paint_slot_shadow(p, hw, hh)

        # ② 磁带内部：卷盘承座 + 磁带卷 + 旋转齿轮盘（皮肤可用 tape_inner 替换静态部分）
        self._paint_inner(p, a)

        # ③ 外壳：画在内部之上 —— 皮肤若提供透明/半透明外壳，就能透出内部结构
        s.draw_asset(p, "tape_shell", shell_rect,
                     lambda pp: self._fallback_shell(pp, hw, hh))

        # ④ 贴纸：封面 + 标题/副标题（白纸深字）
        self._paint_label(p)

        # ⑤ 底部标签条与计数器
        self._paint_lcd(p)

        p.restore()

    def _paint_slot_shadow(self, p, hw, hh):
        """磁带与底座槽位交界处的投影：让磁带看起来是"插进去"的，而不是悬空。"""
        grad = QLinearGradient(0, hh - 16, 0, hh + 36)
        grad.setColorAt(0.0, QColor(0, 0, 0, 145))
        grad.setColorAt(0.55, QColor(0, 0, 0, 60))
        grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRect(QRectF(-hw * 0.97, hh - 16, hw * 1.94, 52))

    def _fallback_shell(self, pp, hw, hh):
        """矢量兜底外壳：透明塑料壳 + 螺丝 + 定位槽（与 tape_shell.svg 保持一致）。"""
        pp.setPen(QPen(QColor(138, 168, 184, 205), 2.5))
        pp.setBrush(QColor(255, 255, 255, 40))
        pp.drawRoundedRect(QRectF(-hw + 2, -hh + 2, 2 * hw - 4, 2 * hh - 4), 18, 18)
        pp.setPen(QPen(QColor(255, 255, 255, 110), 1.4))
        pp.setBrush(Qt.NoBrush)
        pp.drawRoundedRect(QRectF(-hw + 10, -hh + 10, 2 * hw - 20, 2 * hh - 20), 13, 13)
        # 左侧斜向反光（塑料质感）
        pp.setPen(Qt.NoPen)
        pp.setBrush(QColor(255, 255, 255, 26))
        pp.drawPolygon(QPolygonF([QPointF(-hw + 26, -hh + 40), QPointF(-hw + 26, hh - 106),
                                  QPointF(-hw + 52, hh - 132), QPointF(-hw + 52, -hh + 30)]))
        # 四角与底部中央螺丝
        for sx, sy in ((-hw + 26, -hh + 22), (hw - 26, -hh + 22),
                       (-hw + 26, hh - 22), (hw - 26, hh - 22), (0, hh - 22)):
            pp.setPen(Qt.NoPen)
            pp.setBrush(QColor(168, 188, 200, 235))
            pp.drawEllipse(QPointF(sx, sy), 5.0, 5.0)
            pp.setPen(QPen(QColor(95, 114, 128, 210), 1.4))
            pp.drawLine(QPointF(sx - 3.4, sy), QPointF(sx + 3.4, sy))
        # 底部定位槽
        pp.setPen(Qt.NoPen)
        pp.setBrush(QColor(70, 88, 96, 108))
        pp.drawRoundedRect(self.SLOT_RECT, 4, 4)

    def _paint_label(self, p):
        """白色标签纸 + 封面 + 标题/副标题（白纸深字，对应实物磁带的印刷贴纸）。"""
        s = self.skin
        lab = self.LABEL_RECT
        p.setPen(QPen(QColor(200, 192, 174), 1.6))
        p.setBrush(QColor("#f8f4e9"))
        p.drawRoundedRect(lab, 9, 9)
        p.setPen(QPen(QColor(0, 0, 0, 20), 1.0))          # 贴纸上的浅色印线
        for i in (1, 3):
            y = lab.top() + lab.height() * i / 4.0
            p.drawLine(QPointF(lab.left() + 12, y), QPointF(lab.right() - 12, y))

        # 封面（居中偏上）
        cs = self.COVER_SIZE
        cr = QRect(-cs // 2, self.COVER_CY - cs // 2, cs, cs)
        if self.cover_pixmap is not None:
            pm = self.cover_pixmap.scaled(cs - 6, cs - 6, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.setPen(Qt.NoPen)
            p.drawPixmap(cr.x() + (cs - pm.width()) // 2, cr.y() + (cs - pm.height()) // 2, pm)
        else:
            def fb(pp):
                pp.setPen(QPen(QColor(200, 192, 174), 2))
                pp.setBrush(QColor("#efe6d4"))
                pp.drawRoundedRect(cr.adjusted(1, 1, -1, -1), 8, 8)
                for i, col in enumerate((QColor("#2a6485"), QColor("#c25e3d"))):
                    pp.setPen(Qt.NoPen)
                    pp.setBrush(col)
                    pp.drawRect(cr.x() + 10, cr.y() + 34 + i * 12, cs - 20, 7)
                f = QFont()
                f.setPointSize(30)
                pp.setFont(f)
                pp.setPen(QColor("#5a5245"))
                pp.drawText(cr.adjusted(0, 58, 0, 46), Qt.AlignHCenter | Qt.AlignVCenter, "♪")
                f2 = QFont()
                f2.setPointSize(7)
                pp.setFont(f2)
                pp.setPen(QColor("#8a7f6d"))
                pp.drawText(cr.adjusted(0, 104, 0, 20), Qt.AlignHCenter | Qt.AlignVCenter, "N O   C O V E R")
            s.draw_asset(p, "cover_placeholder", cr, fb)
        p.setPen(QPen(QColor(150, 142, 124), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(cr.adjusted(-3, -3, 3, 3), 10, 10)

        # 标题 / 副标题（写在标签纸上）
        # 注意：15.5pt 粗体的中文实际排版高度约 24px，用 20px 的矩形配 AlignBottom 会
        # 向上溢出、顶进封面下缘（表现为"标题的头顶被封面切掉"）。改用略高的矩形 + 垂直居中。
        fm = QFont()
        fm.setPointSizeF(15.5)
        fm.setBold(True)
        p.setFont(fm)
        p.setPen(s.color("text_title"))
        title_w = self.fontMetrics().horizontalAdvance(self.title_text)
        shown = self.fontMetrics().elidedText(self.title_text, Qt.ElideRight, 420) if title_w > 420 else self.title_text
        p.drawText(QRectF(-210, -28, 420, 26), Qt.AlignHCenter | Qt.AlignVCenter, shown)
        f2 = QFont()
        f2.setPointSizeF(11.0)
        p.setFont(f2)
        p.setPen(s.color("text_sub"))
        sub_w = self.fontMetrics().horizontalAdvance(self.sub_text)
        shown_sub = self.fontMetrics().elidedText(self.sub_text, Qt.ElideRight, 430) if sub_w > 430 else self.sub_text
        p.drawText(QRectF(-210, 0, 420, 20), Qt.AlignHCenter | Qt.AlignVCenter, shown_sub)

    def _paint_inner(self, p, a):
        """磁带内部结构：观察窗承座 + 两个磁带卷 + 旋转齿轮盘。

        静态部分可由皮肤资源 ``tape_inner`` 整体替换（画布 520x356，与外壳同坐标），
        动态部分（带卷大小、齿轮盘旋转）始终由程序绘制，因此自定义外壳仍保留动画。
        """
        s = self.skin
        win = self.WINDOW_RECT

        def fb(pp):
            # 窗内暗色承座
            pp.setPen(QPen(QColor(70, 86, 96, 175), 1.6))
            pp.setBrush(QColor(30, 36, 42, 235))
            pp.drawRoundedRect(win, 9, 9)
            # 左右导带柱
            pp.setPen(Qt.NoPen)
            pp.setBrush(QColor(58, 68, 76, 235))
            for sx in (win.left() + 16, win.right() - 16):
                pp.drawEllipse(QPointF(sx, self.HUB_Y - 8), 5.5, 5.5)

        s.draw_asset(p, "tape_inner", QRectF(win), fb)

        p.save()
        p.setClipRect(win.adjusted(1, 1, -1, -1))
        tape_c = QColor("#4e3b2c")
        hub_r = self.HUB_R
        coils = ((-self.HUB_X, a.left_coil(), a.left_angle),
                 (self.HUB_X, a.right_coil(), a.right_angle))

        # 走带：带体从一盘外缘拉到另一盘，走窗口下沿那条道
        p.setPen(QPen(tape_c, 6, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(QPointF(-self.HUB_X + coils[0][1] - 3, self.HUB_Y + 15),
                   QPointF(self.HUB_X - coils[1][1] + 3, self.HUB_Y + 15))

        # 磁带卷：卷芯半径固定，卷厚随进度变化（左盘收带变厚、右盘放带变薄）
        for hx, coil, _ang in coils:
            path = QPainterPath()
            path.addEllipse(QPointF(hx, self.HUB_Y), coil, coil)
            path.addEllipse(QPointF(hx, self.HUB_Y), hub_r - 1.0, hub_r - 1.0)
            path.setFillRule(Qt.OddEvenFill)
            p.setPen(Qt.NoPen)
            p.fillPath(path, tape_c)
            p.setPen(QPen(QColor(107, 81, 64, 190), 2.0))          # 卷顶层叠高光
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(hx - coil + 2.5, self.HUB_Y - coil + 2.5,
                             (coil - 2.5) * 2, (coil - 2.5) * 2), 55 * 16, 70 * 16)

        # 卷芯齿轮盘（固定盘径，随带速旋转）
        for hx, _coil, ang in coils:
            self._paint_hub(p, hx, self.HUB_Y, hub_r, ang)

        # 走带进度条：就在两个转轴之间，可以直接拖动跳转
        self._paint_progress(p, a)
        p.restore()

    def _paint_progress(self, p, a):
        """观察窗内两转轴之间的走带进度条：拖动预览，松手跳转。"""
        tr = self.PROG_RECT
        dragging = self._drag_frac is not None
        frac = clamp01(self._drag_frac if dragging else a.progress)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 14, 18, 240))                        # 凹槽
        p.drawRoundedRect(tr.adjusted(-1.5, -1.5, 1.5, 1.5), 5.5, 5.5)

        if frac > 0.001:                                           # 已播部分
            fill = QRectF(tr.left() + 1, tr.top() + 1,
                          max(2.5, (tr.width() - 2) * frac), tr.height() - 2)
            grad = QLinearGradient(0, tr.top(), 0, tr.bottom())
            grad.setColorAt(0.0, QColor("#f4d27c"))
            grad.setColorAt(0.5, QColor("#dfae44"))
            grad.setColorAt(1.0, QColor("#b8862a"))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(fill, 4, 4)

        p.setPen(QPen(QColor(255, 255, 255, 28), 1))               # 四等分刻度
        for i in (1, 2, 3):
            x = tr.left() + tr.width() * i / 4.0
            p.drawLine(QPointF(x, tr.top() + 1.5), QPointF(x, tr.bottom() - 1.5))

        cx, cy = tr.left() + tr.width() * frac, tr.center().y()
        knob_r = 7.0 if dragging else 5.5
        if dragging or self._prog_hover:                           # 悬停/拖动时的柔光
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 236, 180, 60 if dragging else 34))
            p.drawEllipse(QPointF(cx, cy), knob_r + 4.5, knob_r + 4.5)
        p.setPen(QPen(QColor(72, 54, 26), 1.5))
        p.setBrush(QColor("#fdf6e5"))
        p.drawEllipse(QPointF(cx, cy), knob_r, knob_r)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 165))                     # 滑钮高光
        p.drawEllipse(QPointF(cx - knob_r * 0.28, cy - knob_r * 0.34), knob_r * 0.3, knob_r * 0.26)

    def _paint_hub(self, p, x, y, r, angle):
        """卷芯齿轮盘：盘径固定，卡爪与内圈随角度旋转（贴图可用 reel_hub 替换）。"""
        asset = self.skin.assets.get("reel_hub")
        if asset is not None:
            kind, obj = asset
            p.save()
            p.translate(x, y)
            p.rotate(math.degrees(angle))
            rect = QRectF(-r, -r, 2 * r, 2 * r)
            try:
                if kind == "svg":
                    obj.render(p, rect)
                else:
                    p.drawPixmap(rect, obj, QRectF(obj.rect()))
                p.restore()
                return
            except Exception:
                p.restore()

        # 盘面（米白塑料，上亮下暗）
        disc = QLinearGradient(x, y - r, x, y + r)
        disc.setColorAt(0.0, QColor(240, 245, 248, 246))
        disc.setColorAt(0.55, QColor(224, 233, 238, 246))
        disc.setColorAt(1.0, QColor(198, 210, 219, 246))
        p.setPen(QPen(QColor(140, 156, 166, 220), max(1.4, r * 0.075)))
        p.setBrush(QBrush(disc))
        p.drawEllipse(QPointF(x, y), r, r)

        # 6 个轴向卡爪（实物卷芯内壁的卡齿，随旋转可见）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(196, 208, 218, 235))
        for k in range(6):
            ang = angle + k * math.pi / 3
            p.save()
            p.translate(x + math.cos(ang) * r * 0.45, y + math.sin(ang) * r * 0.45)
            p.rotate(math.degrees(ang) + 90.0)
            p.drawRoundedRect(QRectF(-r * 0.09, -r * 0.18, r * 0.18, r * 0.36), r * 0.045, r * 0.045)
            p.restore()

        # 内圈 + 深色轴孔 + 中心轴
        p.setPen(QPen(QColor(150, 165, 175, 225), max(1.6, r * 0.085)))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(x, y), r * 0.62, r * 0.62)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(40, 48, 56, 240))
        p.drawEllipse(QPointF(x, y), r * 0.34, r * 0.34)
        p.setBrush(QColor(228, 234, 238, 246))
        p.drawEllipse(QPointF(x, y), r * 0.165, r * 0.165)

    def _paint_lcd(self, p):
        """底部标签条（型号字样）+ 计数器 LCD。"""
        s = self.skin
        p.setPen(QPen(QColor(200, 192, 174), 1.4))
        p.setBrush(QColor("#f8f4e9"))
        p.drawRoundedRect(self.STRIP_RECT, 7, 7)
        f0 = QFont()
        f0.setPointSizeF(9.0)
        f0.setBold(True)
        p.setFont(f0)
        p.setPen(QColor(126, 120, 106))
        p.drawText(QRectF(self.STRIP_RECT.left() + 12, self.STRIP_RECT.top(), 70, self.STRIP_RECT.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, "RT-90")
        p.drawText(QRectF(self.STRIP_RECT.right() - 82, self.STRIP_RECT.top(), 70, self.STRIP_RECT.height()),
                   Qt.AlignRight | Qt.AlignVCenter, "HiFi")

        lcd = self.LCD_RECT
        p.setPen(QPen(QColor(52, 62, 54), 1.4))
        p.setBrush(s.color("lcd_bg"))
        p.drawRoundedRect(lcd, 5, 5)
        f3 = QFont("Courier New")
        f3.setPointSizeF(9.5)
        f3.setBold(True)
        p.setFont(f3)
        if self._drag_pos is not None:                            # 拖动进度条时显示预览时间
            p.setPen(QColor("#ffd98a"))
            cur = self._drag_pos
        else:
            p.setPen(s.color("lcd_fg"))
            cur = self.pos_now if (self.playing or self.pos_now > 0) else 0.0
        tot = self.dur_total
        txt = (f"{int(cur // 60):02d}:{int(cur % 60):02d} / {int(tot // 60):02d}:{int(tot % 60):02d}"
               if tot > 0 else "--:-- / --:--")
        p.drawText(lcd.adjusted(3, 0, -3, 0), Qt.AlignHCenter | Qt.AlignVCenter, txt)

    # ---------------- 图标（矢量兜底） ----------------
    def _glyph(self, p, key, r, c):
        cx, cy = r.center().x(), r.center().y()
        s = min(r.width(), r.height()) * 0.30
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if key == "btn_play":
            path = QPainterPath()
            path.moveTo(cx - s * 0.85, cy - s)
            path.lineTo(cx + s, cy)
            path.lineTo(cx - s * 0.85, cy + s)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPath(path)
        elif key == "btn_pause":
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            wdt, gap = s * 0.54, s * 0.24
            for dx in (-(gap * 0.5 + wdt), gap * 0.5):     # 两条竖线，中间留出清晰间隙
                p.drawRoundedRect(QRectF(cx + dx, cy - s, wdt, 2 * s), wdt * 0.34, wdt * 0.34)
        elif key == "btn_prev":
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(cx - s * 1.35, cy - s, s * 0.68, 2 * s, 2, 2)   # 竖条
            path = QPainterPath()
            path.moveTo(cx + s * 1.05, cy - s)
            path.lineTo(cx - s * 0.45, cy)
            path.lineTo(cx + s * 1.05, cy + s)
            path.closeSubpath()
            p.drawPath(path)
        elif key == "btn_next":
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            path = QPainterPath()
            path.moveTo(cx - s * 1.05, cy - s)
            path.lineTo(cx + s * 0.45, cy)
            path.lineTo(cx - s * 1.05, cy + s)
            path.closeSubpath()
            p.drawPath(path)
            p.drawRoundedRect(cx + s * 0.67, cy - s, s * 0.68, 2 * s, 2, 2)   # 竖条
        elif key == "btn_stop":
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(cx - s * 0.9, cy - s * 0.9, 1.8 * s, 1.8 * s, 3, 3)
        elif key == "btn_loop":
            # 循环：圆角矩形回路 + 两个方向箭头
            p.setPen(QPen(c, max(2.4, s * 0.26), Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(cx - s * 1.05, cy - s * 0.78, 2.1 * s, 1.56 * s, s * 0.42, s * 0.42)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            for (ax, ay, dx, dy) in ((cx + s * 1.05, cy - s * 0.78, 0, 1),   # 右上→右
                                     (cx - s * 1.05, cy + s * 0.78, 0, -1)):  # 左下→左
                path = QPainterPath()
                if dx == 0 and dy > 0:      # 朝下的箭头（右上拐角）
                    path.moveTo(ax - s * 0.42, ay - s * 0.3)
                    path.lineTo(ax + s * 0.5, ay + s * 0.18)
                    path.lineTo(ax - s * 0.06, ay + s * 0.42)
                else:                        # 朝上的箭头（左下拐角）
                    path.moveTo(ax + s * 0.42, ay + s * 0.3)
                    path.lineTo(ax - s * 0.5, ay - s * 0.18)
                    path.lineTo(ax + s * 0.06, ay - s * 0.42)
                path.closeSubpath()
                p.drawPath(path)
        elif key == "btn_sequence":
            # 顺序播放：右向箭头 + 终止竖线
            p.setPen(QPen(c, max(2.8, s * 0.30), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(cx - s * 1.02, cy, cx + s * 0.42, cy)
            p.drawLine(cx + s * 1.02, cy - s * 0.85, cx + s * 1.02, cy + s * 0.85)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            path = QPainterPath()
            path.moveTo(cx + s * 0.10, cy - s * 0.62)
            path.lineTo(cx + s * 0.90, cy)
            path.lineTo(cx + s * 0.10, cy + s * 0.62)
            path.closeSubpath()
            p.drawPath(path)
        elif key == "btn_repeat_one":
            # 单曲循环：回路 + 中央数字 1
            p.setPen(QPen(c, max(2.4, s * 0.26), Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(cx - s * 1.05, cy - s * 0.78, 2.1 * s, 1.56 * s, s * 0.42, s * 0.42)
            p.setPen(QPen(c, max(2.0, s * 0.20), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(cx - s * 0.16, cy - s * 0.46, cx - s * 0.16, cy + s * 0.46)   # 数字 1 的主笔
            p.drawLine(cx - s * 0.48, cy - s * 0.20, cx - s * 0.16, cy - s * 0.46)   # 数字 1 的起笔
        elif key == "btn_shuffle":
            # 随机播放：两条交叉走向线 + 两个箭头
            p.setPen(QPen(c, max(2.6, s * 0.30), Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawLine(cx - s * 1.05, cy - s * 0.78, cx - s * 0.15, cy - s * 0.78)
            p.drawLine(cx - s * 0.15, cy - s * 0.78, cx + s * 0.72, cy + s * 0.78)
            p.drawLine(cx - s * 1.05, cy + s * 0.78, cx - s * 0.15, cy + s * 0.78)
            p.drawLine(cx - s * 0.15, cy + s * 0.78, cx + s * 0.72, cy - s * 0.78)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            for ax, ay, ddy in ((cx + s * 0.72, cy - s * 0.78, 1.0),
                                (cx + s * 0.72, cy + s * 0.78, -1.0)):
                path = QPainterPath()
                path.moveTo(ax - s * 0.52, ay - ddy * s * 0.20)
                path.lineTo(ax + s * 0.44, ay)
                path.lineTo(ax - s * 0.52, ay + ddy * s * 0.20)
                path.closeSubpath()
                p.drawPath(path)
        elif key == "btn_console":
            # 调音台推子图标：三条滑槽 + 三个滑块
            p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap))
            for i in range(3):
                x = cx + (i - 1) * s * 0.66
                p.drawLine(QPointF(x, cy - s * 0.95), QPointF(x, cy + s * 0.95))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            for i, t in enumerate((-0.42, 0.38, -0.06)):
                x = cx + (i - 1) * s * 0.66
                p.drawRoundedRect(QRectF(x - s * 0.28, cy + t * s - s * 0.15,
                                         s * 0.56, s * 0.3), 1.5, 1.5)
        elif key == "win_min":
            p.setPen(QPen(c, 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(cx - s * 1.1, cy + s * 0.55, cx + s * 1.1, cy + s * 0.55)
        elif key == "win_menu":
            p.setPen(QPen(c, 2.8, Qt.SolidLine, Qt.RoundCap))
            for dy in (-s * 0.75, 0, s * 0.75):
                p.drawLine(cx - s * 1.05, cy + dy, cx + s * 1.05, cy + dy)
        elif key == "win_list":
            # 文档图标（对应图一右下角的"文件"按钮）
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(cx - s * 0.85, cy - s * 1.05, 1.7 * s, 2.1 * s, 3, 3)
            p.setPen(QPen(self.skin.color("chrome_bg"), 2.2, Qt.SolidLine, Qt.RoundCap))
            for dy in (-s * 0.45, -s * 0.05, s * 0.35):
                wdt = s * (1.0 if dy != s * 0.35 else 0.6)
                p.drawLine(cx - s * 0.45, cy + dy, cx - s * 0.45 + wdt, cy + dy)
        elif key == "win_close":
            p.setPen(QPen(c, 2.8, Qt.SolidLine, Qt.RoundCap))
            d = s * 0.95
            p.drawLine(cx - d, cy - d, cx + d, cy + d)
            p.drawLine(cx + d, cy - d, cx - d, cy + d)
        p.restore()

    # ---------------- 交互 ----------------
    def _hit(self, pos):
        for r, k in self._hit_map:
            if r.contains(pos):
                return k
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        pos = e.position()
        if self._prog_hit(pos):           # 抓进度条优先：不触发拖窗口
            self._drag_frac = self._prog_frac_at(pos)
            self._drag_pos = self._drag_frac * self.dur_total
            self._anim.progress = self._drag_frac      # 拖动时两盘卷径实时变化
            self.setCursor(Qt.SizeHorCursor)
            self.update()
            return
        if self._knob_hit(pos):           # 音量旋钮：绕中心拖动（增量式，指针不突跳）
            self._knob_ref_ang = self._knob_angle(pos)
            self.setCursor(Qt.PointingHandCursor)
            self.update(self._knob_update_rect())
            return
        k = self._hit(pos.toPoint())
        if k is None:
            # 没有标题栏：按住磁带/底座/空白处即拖动整个窗口
            self._pressed_key = None
            self._drag_off = e.globalPosition().toPoint() - self.window().pos()
            self.setCursor(Qt.ClosedHandCursor)
            return
        self._pressed_key = k
        self.update()
        act = self.KEY2ACTION[k]
        if k == "win_menu":
            self.menu_requested.emit(QPointF(e.globalPosition()))
        elif act:
            self.action.emit(act)

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self._drag_frac is not None:               # 正在拖动进度条
            self._drag_frac = self._prog_frac_at(pos)
            self._drag_pos = self._drag_frac * self.dur_total
            self._anim.progress = self._drag_frac
            self.update()
            return
        if self._knob_ref_ang is not None:            # 正在拧音量旋钮
            delta = self._knob_angle(pos) - self._knob_ref_ang
            delta = (delta + 180.0) % 360.0 - 180.0   # 归一到 -180..180，跨 0° 也不会跳
            self.set_volume(self.volume + delta / self.KNOB_SPAN)
            self._knob_ref_ang = self._knob_angle(pos)
            return
        if self._drag_off is not None:
            self.window().move(e.globalPosition().toPoint() - self._drag_off)
            return
        k = self._hit(pos.toPoint())
        over_prog = self._prog_hit(pos)
        over_knob = self._knob_hit(pos)
        if k != self._hover_key:
            self._hover_key = k
            self.update()                              # 按钮 hover 变化：静态层缓存要重建
        if over_prog != self._prog_hover:
            self._prog_hover = over_prog
            self.update(self._tape_rect())             # 进度条画在观察窗里
        if over_knob != self._knob_hover:
            self._knob_hover = over_knob
            self.update(self._knob_update_rect())
        if k:
            self.setCursor(Qt.PointingHandCursor)
        elif over_knob:
            self.setCursor(Qt.PointingHandCursor)      # 旋钮可拧
        elif over_prog:
            self.setCursor(Qt.SizeHorCursor)          # 提示可左右拖动
        elif self._tape_hit(pos.toPoint()):
            self.setCursor(Qt.OpenHandCursor)         # 提示磁带区可拖动窗口
        else:
            self.setCursor(Qt.ArrowCursor)

    def wheelEvent(self, ev):
        """滚轮微调音量（每次 ±2%），比绕圈拧更省事。"""
        steps = ev.angleDelta().y() / 120.0
        if abs(steps) > 1e-6:
            self.set_volume(self.volume + steps * 0.02)
            ev.accept()
            return
        super().wheelEvent(ev)

    def mouseReleaseEvent(self, e):
        if self._knob_ref_ang is not None:
            self._knob_ref_ang = None
            self.setCursor(Qt.PointingHandCursor if self._knob_hit(e.position())
                           else Qt.ArrowCursor)
            self.update(self._knob_update_rect())
            return
        if self._drag_frac is not None:
            frac, self._drag_frac = self._drag_frac, None
            self._drag_pos = None
            self.setCursor(Qt.SizeHorCursor if self._prog_hit(e.position()) else Qt.ArrowCursor)
            if frac is not None and self.dur_total > 0:
                self.seek_requested.emit(frac * self.dur_total)
            self.update()
            return
        if self._pressed_key is not None:
            self._pressed_key = None
            self.update()
        if self._drag_off is not None:
            self._drag_off = None
            self.setCursor(Qt.OpenHandCursor if self._tape_hit(e.position().toPoint())
                           else Qt.ArrowCursor)

    def _to_tape_local(self, pos):
        """把窗口坐标换算成磁带局部坐标（进度条几何都在磁带坐标系里）。"""
        r = self.skin.rect("tape")
        if r is None:
            return QPointF(pos)
        return QPointF(pos.x() - r.center().x(), pos.y() - r.center().y())

    def _prog_hit(self, pos):
        """进度条命中判定：有曲目才响应。"""
        if self.dur_total <= 0:
            return False
        lp = self._to_tape_local(pos)
        return self.PROG_HIT.contains(lp)

    def _prog_frac_at(self, pos):
        lp = self._to_tape_local(pos)
        tr = self.PROG_RECT
        return clamp01((lp.x() - tr.left()) / max(1.0, tr.width()))

    def _tape_hit(self, pos):
        r = self.skin.rect("tape")
        return bool(r and r.contains(pos))

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Space:
            self.action.emit("toggle_play")
        elif k in (Qt.Key_Right,):
            self.action.emit("seek_fwd")
        elif k in (Qt.Key_Left,):
            self.action.emit("seek_back")
        elif k in (Qt.Key_N, Qt.Key_Down):
            self.action.emit("next")
        elif k in (Qt.Key_P, Qt.Key_Up):
            self.action.emit("prev")
        elif k == Qt.Key_L:
            self.action.emit("loop")
        elif k in (Qt.Key_Equal, Qt.Key_Plus):        # = / + 音量加
            self.set_volume(self.volume + 0.05)
        elif k in (Qt.Key_Minus, Qt.Key_Underscore):  # - / _ 音量减
            self.set_volume(self.volume - 0.05)
        elif k == Qt.Key_M:                           # M 静音/恢复
            if self.volume > 0.001:
                self._vol_before_mute = self.volume
                self.set_volume(0.0)
            else:
                self.set_volume(getattr(self, "_vol_before_mute", 0.8))
        else:
            super().keyPressEvent(ev)
