"""皮肤预览工具：把每个皮肤包在"播放中"状态下的磁带特写并排渲染，便于对照调皮肤。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tests\\skin_preview.py [输出图片路径]
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "tests", "shots", "10_skin_compare.png")
    app = QApplication(sys.argv[:1])

    import main as m
    from app.skin_loader import Skin

    p = m.PlayerApp()
    # 摆一个确定性的演示状态：标题、进度 25%、列表循环
    p.deck.set_labels("不可错过的歌", "YG · Side A")
    p.deck.set_progress(3.0, 12.0)
    p.deck.set_playing(True)
    p.deck.set_loop_mode(2)

    skins = Skin.list_packs(os.path.join(ROOT, "skins")) or ["default"]
    crop = QRect(196, 118, 568, 400)          # 磁带特写（layout.tape 220,142,520,356 外扩）
    W, H, GAP, TOP = crop.width(), crop.height(), 8, 28

    canvas = QImage(W * len(skins) + GAP * (len(skins) - 1), H + TOP, QImage.Format_ARGB32)
    canvas.fill(QColor("#cdd5dd"))
    pt = QPainter(canvas)
    f = pt.font()
    f.setPointSize(10)
    f.setBold(True)
    pt.setFont(f)
    for i, name in enumerate(skins):
        p.apply_skin(name)
        p.deck.repaint()
        x = i * (W + GAP)
        pt.drawPixmap(x, TOP, p.deck.grab(crop))
        pt.setPen(QColor("#2f3742"))
        pt.drawText(QRect(x, 2, W, 22), Qt.AlignHCenter | Qt.AlignVCenter, name)
    pt.end()
    canvas.save(out)
    print("saved", out, "skins:", skins)


if __name__ == "__main__":
    main()
