"""无边框 + 半透明背景 + 内容投影的悬浮窗口基类（漂浮物质感）。

投影不用挂在子部件上的 QGraphicsDropShadowEffect —— 那种做法会在内容**每次重绘**时
重新做一遍整窗高斯模糊，动画帧率会被直接拖垮。这里改成：
把内容轮廓一次性"离线模糊"成一张位图缓存起来，动画帧只需要贴图。
内容形状大改（换皮肤、改尺寸）时调用 rebuild_shadow() 重建。
"""
from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene,
                               QVBoxLayout, QWidget)


class ShadowWindow(QWidget):
    def __init__(self, margin=18, blur=30, color=(0, 0, 0, 95), parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._margin = margin
        self._blur = blur
        self._color = QColor(*color)
        self._offset_y = 2               # 投影略微下移，更像"浮在桌面上"
        self._content = None
        self._shadow = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(margin, margin, margin, margin)
        lay.setSpacing(0)

    def add_content(self, w: QWidget):
        """放入内容部件；投影由本窗口负责，不再给内容挂图形效果。"""
        self._content = w
        self.layout().addWidget(w)
        QTimer.singleShot(0, self.rebuild_shadow)

    # ---------------- 投影缓存 ----------------
    def rebuild_shadow(self):
        """按当前内容轮廓重建投影位图。只在内容形状大改时调用（动画帧不调用）。"""
        if self._content is None or self.width() <= 2 or self.height() <= 2:
            return
        img = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        pt = QPainter(img)
        pt.setRenderHint(QPainter.Antialiasing)
        self._content.render(pt, QPoint(self._margin, self._margin))
        pt.end()

        shape = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
        shape.fill(0)
        sp = QPainter(shape)
        sp.drawImage(0, 0, img)
        sp.setCompositionMode(QPainter.CompositionMode_SourceIn)   # 用内容 alpha 当遮罩，染成投影色
        sp.fillRect(shape.rect(), self._color)
        sp.end()

        self._shadow = self._blur_image(shape)

    def _blur_image(self, src: QImage) -> QPixmap:
        """离线模糊一次：QGraphicsBlurEffect 挂在临时图元上，经场景渲染到图片。

        失败（少数平台没有该效果）时退化为"多层偏移"软阴影，比不做阴影好、比实时模糊快。
        """
        try:
            pm = QPixmap.fromImage(src)
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(pm)
            eff = QGraphicsBlurEffect()
            eff.setBlurRadius(self._blur_radius())
            item.setGraphicsEffect(eff)
            scene.addItem(item)
            out = QImage(src.size(), QImage.Format_ARGB32_Premultiplied)
            out.fill(0)
            p = QPainter(out)
            p.setRenderHint(QPainter.Antialiasing)
            scene.render(p, QRectF(out.rect()), QRectF(pm.rect()))
            p.end()
            scene.clear()
            return QPixmap.fromImage(out)
        except Exception:
            pm = QPixmap.fromImage(src)
            out = QPixmap(src.size())
            out.fill(Qt.transparent)
            p = QPainter(out)
            for step in range(6):                       # 同心偏移叠出软边
                p.setOpacity(0.16)
                d = step * 2
                p.drawPixmap(d - 5, d - 5 + self._offset_y, pm)
                p.drawPixmap(5 - d, 5 - d + self._offset_y, pm)
            p.end()
            return out

    def _blur_radius(self):
        return self._blur

    # ---------------- 绘制 ----------------
    def paintEvent(self, ev):
        if self._shadow is not None:
            p = QPainter(self)
            p.drawPixmap(0, self._offset_y, self._shadow)
            p.end()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._shadow is not None:
            QTimer.singleShot(0, self.rebuild_shadow)

    def showEvent(self, ev):
        super().showEvent(ev)
        if self._shadow is None:
            QTimer.singleShot(0, self.rebuild_shadow)
