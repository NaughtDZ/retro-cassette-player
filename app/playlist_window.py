"""播放列表：独立悬浮窗，练习册风格（白纸网格 + 红色页边线 + 手写体）。

对应参考图二：横向蓝色格线、纵向分栏、左侧双红线、行号 "1."，当前曲目用黄色荧光笔高亮。
"""
from PySide6.QtCore import Qt, QRect, QPointF, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea

from .shadow_window import ShadowWindow


def hand_font(size_pt: float, bold=False) -> QFont:
    f = QFont("KaiTi")
    f.setFamilies(["Kaiti SC", "楷体", "KaiTi"])
    f.setPointSizeF(size_pt)
    f.setBold(bold)
    return f


PAPER_BG = "#fffdf4"
GRID_LINE = QColor("#b9d2e8")
BORDER_BLUE = QColor("#7fa8c9")
MARGIN_RED = QColor("#e2574c")
TEXT_MAIN = QColor("#2c3138")
TEXT_DIM = QColor("#6a7683")
MARK_YELLOW = QColor(255, 228, 122)


class HeaderBar(QWidget):
    """纸面顶部：标题（荧光笔高亮）+ 曲目数 + 关闭按钮；按住可拖动窗口。"""
    close_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(46)
        self.count_text = ""
        self._drag_off = None
        self.close_rect = QRect(0, 0, 30, 28)

    def set_count(self, text):
        self.count_text = text
        self.update()

    def _layout_close(self):
        self.close_rect = QRect(self.width() - 34, 9, 26, 26)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(PAPER_BG))
        # 标题 + 荧光笔
        f = hand_font(15.5, bold=True)
        p.setFont(f)
        title = "♪ 播放列表"
        tw = p.fontMetrics().horizontalAdvance(title)
        p.setPen(Qt.NoPen)
        p.setBrush(MARK_YELLOW)
        p.drawRect(QRect(14, h // 2 - 13, tw + 16, 26))
        p.setPen(TEXT_MAIN)
        p.drawText(QRect(20, 0, tw, h), Qt.AlignVCenter | Qt.AlignLeft, title)
        # 曲目数
        if self.count_text:
            f2 = hand_font(11)
            p.setFont(f2)
            p.setPen(TEXT_DIM)
            cw = p.fontMetrics().horizontalAdvance(self.count_text)
            p.drawText(QRect(w - 34 - cw - 10, 0, cw, h), Qt.AlignVCenter | Qt.AlignRight, self.count_text)
        # 关闭按钮
        self._layout_close()
        cr = self.close_rect
        p.setPen(QPen(TEXT_DIM, 2.2, Qt.SolidLine, Qt.RoundCap))
        d = 6.5
        cx, cy = cr.center().x(), cr.center().y()
        p.drawLine(cx - d, cy - d, cx + d, cy + d)
        p.drawLine(cx + d, cy - d, cx - d, cy + d)
        p.end()

    def mousePressEvent(self, e):
        if self.close_rect.contains(e.position().toPoint()):
            self.close_clicked.emit()
            return
        self._drag_off = e.globalPosition().toPoint() - self.window().pos()

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and (e.buttons() & Qt.LeftButton):
            self.window().move(e.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, ev):
        self._drag_off = None


class SheetWidget(QWidget):
    """练习册纸面：格线 + 分栏 + 行号；双击某行播放该曲。"""
    play_requested = Signal(int)

    ROW_H = 30
    NUM_COL = 56          # 行号列宽
    DUR_COL = 92          # 时长列宽（右侧）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list = []
        self.current = -1
        self.setMinimumWidth(470)
        self._resize()

    def set_rows(self, tracks, current):
        self.rows = list(tracks)
        self.current = current
        self._resize()
        self.update()

    def set_current(self, current):
        """只切换当前曲目：只重绘受影响的两行（上千首的列表也不卡）。"""
        old, self.current = self.current, current
        for i in (old, current):
            if 0 <= i < len(self.rows):
                self.update(0, i * self.ROW_H, self.width(), self.ROW_H)

    def _resize(self):
        h = max(240, len(self.rows) * self.ROW_H + 6)
        self.setFixedHeight(h)

    @staticmethod
    def fmt_dur(sec):
        if not sec or sec <= 0:
            return "--:--"
        sec = int(round(sec))
        return f"{sec // 60}:{sec % 60:02d}"

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        clip = ev.rect()
        row_h = self.ROW_H
        # 只处理可见行：列表可能有上千首，全量遍历会因为逐行文字排版把界面拖住
        first = max(0, clip.top() // row_h)
        last = min(len(self.rows), clip.bottom() // row_h + 2)
        bottom_y = min(self.height(), (last + 1) * row_h)

        p.fillRect(clip, QColor(PAPER_BG))

        # 横向格线（限定可见范围）
        p.setPen(QPen(GRID_LINE, 1.2))
        for i in range(first, last + 1):
            y = i * row_h
            p.drawLine(clip.left(), y, clip.right(), y)
        if bottom_y > last * row_h:
            p.drawLine(clip.left(), bottom_y, clip.right(), bottom_y)

        # 纵向分栏线（行号列 / 时长列）
        x_num, x_dur = self.NUM_COL, w - self.DUR_COL
        for x in (x_num, x_dur):
            p.setPen(QPen(GRID_LINE, 1.4))
            p.drawLine(x, clip.top(), x, clip.bottom())

        # 左侧双红线（页边线）
        p.setPen(QPen(MARGIN_RED, 2.0))
        p.drawLine(18, clip.top(), 18, clip.bottom())
        p.setPen(QPen(MARGIN_RED, 1.0))
        p.drawLine(23, clip.top(), 23, clip.bottom())

        # 外框
        p.setPen(QPen(BORDER_BLUE, 1.6))
        p.drawRect(-1, -1, w + 2, self.height() + 2)

        # 行内容（仅可见行）
        f_num = hand_font(12)
        f_dur = hand_font(11)
        fm = p.fontMetrics()
        tw_max = x_dur - x_num - 16
        for i in range(first, last):
            t = self.rows[i]
            y = i * row_h
            if i == self.current:
                p.setPen(Qt.NoPen)
                p.setBrush(MARK_YELLOW)
                p.drawRect(26, y + 3, w - 30, row_h - 6)   # 荧光笔高亮当前行
            p.setFont(f_num)
            p.setPen(QColor("#d43c31") if i == self.current else TEXT_MAIN)
            num = f"{i + 1}."
            p.drawText(QRect(0, y, x_num - 6, row_h), Qt.AlignRight | Qt.AlignVCenter, num)
            if i == self.current:      # 红色小三角标记
                tri = QRect(x_num - 14, y + row_h // 2 - 5, 9, 10)
                path = QPainterPath()
                path.moveTo(tri.left(), tri.top())
                path.lineTo(tri.right(), tri.center().y())
                path.lineTo(tri.left(), tri.bottom())
                path.closeSubpath()
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#d43c31"))
                p.drawPath(path)
            title = t.title or "（未知标题）"
            fm = p.fontMetrics()
            shown = fm.elidedText(title, Qt.ElideRight, tw_max) if \
                fm.horizontalAdvance(title) > tw_max else title
            p.setPen(TEXT_MAIN)
            p.drawText(QRect(x_num + 8, y, tw_max, row_h), Qt.AlignLeft | Qt.AlignVCenter, shown)
            p.setFont(f_dur)
            p.setPen(TEXT_DIM if i != self.current else TEXT_MAIN)
            p.drawText(QRect(x_dur + 6, y, self.DUR_COL - 14, row_h),
                       Qt.AlignRight | Qt.AlignVCenter, self.fmt_dur(t.duration))
        p.end()

    def mouseDoubleClickEvent(self, ev):
        idx = int(ev.position().y() // self.ROW_H)
        if 0 <= idx < len(self.rows):
            self.play_requested.emit(idx)


class PaperWidget(QWidget):
    """纸面容器：标题栏 + 可滚动的练习册。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(470, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = HeaderBar()
        self.sheet = SheetWidget()
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 8px; background: #f0ead9; }"
            "QScrollBar::handle:vertical { background: #c9bda2; min-height: 30px; border-radius: 4px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }")
        scroll.setWidget(self.sheet)
        lay.addWidget(self.header)
        lay.addWidget(scroll, 1)

    def rebuild(self, tracks, current):
        self.sheet.set_rows(tracks, current)
        self.header.set_count(f"{len(tracks)} 首" if tracks else "空")

    def set_current(self, current):
        self.sheet.set_current(current)


class PlaylistWindow(ShadowWindow):
    """独立悬浮播放列表窗口（默认置顶）。"""
    play_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(margin=18, blur=30)
        self.setWindowTitle("播放列表")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.paper = PaperWidget()
        self.add_content(self.paper)
        self.paper.sheet.play_requested.connect(self.play_requested)
        self.paper.header.close_clicked.connect(self.hide)

    def rebuild(self, tracks, current):
        self.paper.rebuild(tracks, current)

    def set_current(self, current):
        self.paper.set_current(current)
