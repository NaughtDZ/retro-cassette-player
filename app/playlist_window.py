"""播放列表：独立悬浮窗，练习册风格（白纸网格 + 红色页边线 + 手写体）。

对应参考图二：横向蓝色格线、纵向分栏、左侧双红线、行号 "1."，当前曲目用黄色荧光笔高亮。
"""
import os

from PySide6.QtCore import Qt, QRect, QRectF, QPointF, Signal, QMimeData
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath, QDrag
from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QMenu

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
SEL_BG = QColor(205, 226, 245)        # 选中行底：淡蓝，和荧光黄分开（当前曲目仍是黄的）
SEL_BAR = QColor("#4a7fb5")           # 选中行在页边空白处的标记条


class HeaderBar(QWidget):
    """纸面顶部：标题（荧光笔高亮）+ 曲目数 + 关闭按钮；按住可拖动窗口。"""
    close_clicked = Signal()

    def __init__(self, skin=None, parent=None):
        super().__init__(parent)
        self.skin = skin
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
        # v1.1.0：顶栏底可换成 playlist_header（拉伸无损）；标题、荧光笔底衬与计数仍是程序绘制
        if self.skin is not None and self.skin.has_asset("playlist_header"):
            self.skin.draw_asset(p, "playlist_header", QRectF(0, 0, w, h))
        else:
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
    files_dropped = Signal(list)              # 从资源管理器拖进来的音频
    reorder_requested = Signal(int, int)       # 拖动排序：(源行, 插入到哪一行之前)
    props_requested = Signal(int)
    open_folder_requested = Signal(int)
    pick_subtitle_requested = Signal(int)
    remove_requested = Signal(list)            # 移出列表：选中的行下标
    key_forwarded = Signal(object)             # 列表没处理的按键，交回主程序（空格/←→/N/P…）

    ROW_H = 30
    NUM_COL = 56          # 行号列宽
    DUR_COL = 92          # 时长列宽（右侧）

    def __init__(self, skin=None, parent=None):
        super().__init__(parent)
        self.skin = skin
        self.rows: list = []
        self.current = -1
        self.sel: set = set()                   # 选中的行下标（单选也是它，size=1）
        self.anchor = None                      # Shift 区间选择的锚点
        self.setAcceptDrops(True)               # 接受从资源管理器拖进来的音频
        self.setFocusPolicy(Qt.StrongFocus)     # 键盘 Delete / Ctrl+A 需要焦点
        self._press_row = None
        self._press_pos = None
        self._drop_at = None                    # 插入位置指示（行号，可为 len）
        self.setMinimumWidth(470)
        self._resize()

    def set_rows(self, tracks, current):
        self.rows = list(tracks)
        self.current = current
        n = len(self.rows)                      # 列表变短后，把越界的选中项丢掉
        self.sel = {i for i in self.sel if 0 <= i < n}
        if self.anchor is not None and not (0 <= self.anchor < n):
            self.anchor = None
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

        # v1.1.0：纸底可换成 playlist_paper（纯色/纹理，拉伸无损）。横格线、分栏线与
        # 左侧页边线仍由程序绘制——它们的条数随行数和窗口尺寸变化，资源化没有意义。
        s = self.skin
        if s is not None and s.has_asset("playlist_paper"):
            s.draw_asset(p, "playlist_paper", QRectF(0, 0, w, self.height()))
        else:
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
            picked = i in self.sel
            if picked:                                      # 选中底（当前曲目会再叠荧光黄）
                p.setPen(Qt.NoPen)
                p.setBrush(SEL_BG)
                p.drawRect(26, y + 3, w - 30, row_h - 6)
            if i == self.current:
                mark = QRectF(26, y + 3, w - 30, row_h - 6)
                if s is not None and s.has_asset("playlist_mark"):
                    s.draw_asset(p, "playlist_mark", mark)         # 荧光笔高亮（可换图）
                else:
                    p.setPen(Qt.NoPen)
                    p.setBrush(MARK_YELLOW)
                    p.drawRect(26, y + 3, w - 30, row_h - 6)      # 荧光笔高亮当前行
            if picked:                                      # 页边空白处的蓝条：压在黄底上也认得出
                p.setPen(Qt.NoPen)
                p.setBrush(SEL_BAR)
                p.drawRect(6, y + 7, 4, row_h - 14)
            p.setFont(f_num)
            p.setPen(QColor("#d43c31") if i == self.current else TEXT_MAIN)
            num = f"{i + 1}."
            p.drawText(QRect(0, y, x_num - 6, row_h), Qt.AlignRight | Qt.AlignVCenter, num)
            if i == self.current:      # 红色小三角标记
                tri = QRect(x_num - 14, y + row_h // 2 - 5, 9, 10)
                if s is not None and s.has_asset("playlist_caret"):
                    s.draw_asset(p, "playlist_caret", QRectF(tri))
                else:
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
        if self._drop_at is not None:            # 拖动排序的插入位置指示线
            dy = min(self._drop_at * row_h, self.height() - 1)
            p.setPen(QPen(QColor("#d43c31"), 2.0))
            p.drawLine(26, dy, w - 4, dy)
        p.end()

    # ---------------- 拖放 / 拖动排序 / 右键 ----------------
    def _row_at(self, pos):
        i = int(pos.y()) // self.ROW_H
        return i if 0 <= i < len(self.rows) else -1

    def _insert_at(self, pos):
        """由鼠标位置算出插入位置。

        落在某行的**上半部** → 插到该行之前；**下半部** → 插到该行之后。
        以前这里一律 +1（"插到这一行之后"），于是最小插入位置是 1，
        **永远没法把曲目拖到第 1 位**——这就是"拖不到第一行"的原因。
        """
        i = int(pos.y()) // self.ROW_H
        frac = (pos.y() - i * self.ROW_H) / float(self.ROW_H)
        at = i if frac < 0.5 else i + 1
        return max(0, min(len(self.rows), at))

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat("application/x-retro-row") or md.hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        md = e.mimeData()
        if md.hasFormat("application/x-retro-row") or md.hasUrls():
            self._drop_at = self._insert_at(e.position())
            self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_at = None
        self.update()

    def dropEvent(self, e):
        md = e.mimeData()
        at, self._drop_at = self._drop_at, None
        if md.hasFormat("application/x-retro-row"):
            try:
                src = int(bytes(md.data("application/x-retro-row")).decode() or -1)
            except ValueError:
                src = -1
            self.reorder_requested.emit(src, at if at is not None else self._insert_at(e.position()))
            self.update()
            e.acceptProposedAction()
            return
        if md.hasUrls():
            from .media_info import AUDIO_EXTS
            files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            files = [f for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXTS]
            if files:
                self.files_dropped.emit(files)
            e.acceptProposedAction()

    # ---------------- 选择（单击 / Ctrl / Shift） ----------------
    def clear_selection(self):
        if self.sel or self.anchor is not None:
            self.sel.clear()
            self.anchor = None
            self.update()

    def select_only(self, row):
        self.sel = {row} if row >= 0 else set()
        self.anchor = row if row >= 0 else None
        self.update()

    def _apply_press(self, row, mods):
        """按下左键时的选择语义，照搬资源管理器：

        * 无修饰键 → 只选这一行
        * Ctrl → 切换这一行（已选则取消），锚点移到它
        * Shift → 从锚点到这一行整段选中（连续按 Shift 仍从同一锚点扩）
        * Ctrl+Shift → 把这一段并进已有选择
        """
        ctrl = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        if shift and self.anchor is not None:
            lo, hi = sorted((self.anchor, row))
            span = set(range(lo, hi + 1))
            self.sel = (self.sel | span) if ctrl else span
        elif ctrl:
            if row in self.sel:
                self.sel.discard(row)
            else:
                self.sel.add(row)
            self.anchor = row
        else:
            self.sel = {row}
            self.anchor = row
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            row = self._row_at(e.position())
            self.setFocus(Qt.MouseFocusReason)
            if row < 0:
                self.clear_selection()          # 点空白 = 取消选择
            else:
                self._apply_press(row, e.modifiers())
            self._press_row = row
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_row is None or self._press_row < 0 or not (e.buttons() & Qt.LeftButton):
            return
        if (e.position().toPoint() - self._press_pos).manhattanLength() < 8:
            return                          # 阈值：避免单击/双击被当成拖动
        row, self._press_row = self._press_row, None
        if self.sel != {row}:
            # 拖动排序一次只搬一行：先把选择收敛到它身上，免得"选了一片却只动了一个"，
            # 也避免移动之后蓝条留在已经换了内容的行上
            self.select_only(row)
        md = QMimeData()
        md.setData("application/x-retro-row", str(row).encode())
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.MoveAction)

    def mouseReleaseEvent(self, e):
        self._press_row = None
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Delete and self.sel:
            self.remove_requested.emit(sorted(self.sel))
            return
        if e.key() == Qt.Key_A and (e.modifiers() & Qt.ControlModifier) and self.rows:
            self.sel = set(range(len(self.rows)))
            self.anchor = 0
            self.update()
            return
        if e.key() == Qt.Key_Escape:
            self.clear_selection()
            return
        # 列表自己能接焦点（要收 Delete/Ctrl+A），但没处理的键必须交回主程序：
        # 否则"点一下列表再按空格暂停"就失灵了
        self.key_forwarded.emit(e)

    def _row_menu(self, row):
        """右键某一行时的菜单。返回 (menu, {名字: QAction})，方便自检时不弹模态框。"""
        n_sel = max(1, len(self.sel))
        multi = n_sel > 1
        m = QMenu(self)
        acts = {
            "props": m.addAction("查看属性…"),
            "dir": m.addAction("打开所在文件夹"),
            "sub": m.addAction("手动选择字幕…"),
        }
        if multi:        # 这三项只对一行有意义，多选时禁用，免得以为是"对全部执行"
            for a in acts.values():
                a.setEnabled(False)
        m.addSeparator()
        acts["remove"] = m.addAction(f"移出列表（{n_sel} 首）" if multi else "移出列表")
        if multi:
            acts["remove"].setShortcut("Del")
        return m, acts

    def contextMenuEvent(self, e):
        row = self._row_at(e.pos())
        if row < 0:                              # 空白处：只提供"全选"
            if not self.rows:
                return
            m = QMenu(self)
            a_all = m.addAction("全选")
            if m.exec(e.globalPos()) == a_all and self.rows:
                self.sel = set(range(len(self.rows)))
                self.anchor = 0
                self.update()
            return
        if row not in self.sel:                  # 右键没点在选中集里 → 先把选择收到这一行
            self.select_only(row)
        m, acts = self._row_menu(row)
        act = m.exec(e.globalPos())
        if act is None:
            return
        if act == acts["props"]:
            self.props_requested.emit(row)
        elif act == acts["dir"]:
            self.open_folder_requested.emit(row)
        elif act == acts["sub"]:
            self.pick_subtitle_requested.emit(row)
        elif act == acts["remove"]:
            self.remove_requested.emit(sorted(self.sel or {row}))

    def mouseDoubleClickEvent(self, ev):
        idx = int(ev.position().y() // self.ROW_H)
        if 0 <= idx < len(self.rows):
            self.play_requested.emit(idx)


class PaperWidget(QWidget):
    """纸面容器：标题栏 + 可滚动的练习册。"""

    def __init__(self, skin=None, parent=None):
        super().__init__(parent)
        self.skin = skin
        self.setFixedSize(470, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.header = HeaderBar(skin)
        self.sheet = SheetWidget(skin)
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

    def set_skin(self, skin):
        """换肤：纸底/顶栏/高亮的资源键都由 skin 决定，改完重绘两个子件。"""
        self.skin = skin
        self.header.skin = skin
        self.sheet.skin = skin
        self.header.update()
        self.sheet.update()


class PlaylistWindow(ShadowWindow):
    """独立悬浮播放列表窗口（默认置顶）。"""
    play_requested = Signal(int)
    files_dropped = Signal(list)
    reorder_requested = Signal(int, int)
    props_requested = Signal(int)
    open_folder_requested = Signal(int)
    pick_subtitle_requested = Signal(int)
    remove_requested = Signal(list)
    key_forwarded = Signal(object)

    def __init__(self, skin=None, parent=None):
        super().__init__(margin=18, blur=30)
        self.skin = skin
        self.setWindowTitle("播放列表")
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.paper = PaperWidget(skin)
        self.add_content(self.paper)
        self.paper.sheet.play_requested.connect(self.play_requested)
        # 拖放 / 排序 / 右键动作直接上抛给主程序处理
        self.paper.sheet.files_dropped.connect(self.files_dropped)
        self.paper.sheet.reorder_requested.connect(self.reorder_requested)
        self.paper.sheet.props_requested.connect(self.props_requested)
        self.paper.sheet.open_folder_requested.connect(self.open_folder_requested)
        self.paper.sheet.pick_subtitle_requested.connect(self.pick_subtitle_requested)
        self.paper.sheet.remove_requested.connect(self.remove_requested)
        self.paper.sheet.key_forwarded.connect(self.key_forwarded)
        self.paper.header.close_clicked.connect(self.hide)

    def rebuild(self, tracks, current):
        self.paper.rebuild(tracks, current)

    def set_current(self, current):
        self.paper.set_current(current)

    def clear_selection(self):
        """列表内容变了（例如刚移出了几行）之后清掉选择，免得蓝条留在错的行上。"""
        self.paper.sheet.clear_selection()
