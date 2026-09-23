"""生成文档/README 用的界面截图（offscreen 渲染，无需显示器）。

输出到 docs/images/：
    hero.png          主界面（磁带机 + 两侧 VU 表 + 底座旋钮/按钮）
    console.png       复古磁带 console 悬浮窗（含分区开关与 POWER）
    playlist.png      练习册风格播放列表
    skin-compare.png  两套内置皮肤对比

用法（项目根目录）：.venv\\Scripts\\python.exe tools\\make_screenshots.py
"""
import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "docs", "images")


def demo_cover(size=512):
    """画一张演示用专辑封面（样本音频没有内嵌封面）。"""
    from PySide6.QtCore import QPointF, QRect, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen, QPixmap

    img = QImage(size, size, QImage.Format_ARGB32)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0.0, QColor("#1d4a66"))
    grad.setColorAt(0.5, QColor("#2a6485"))
    grad.setColorAt(1.0, QColor("#b8573a"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.fillRect(0, 0, size, size, grad)
    p.setPen(QPen(QColor(255, 255, 255, 40), size * 0.010))
    p.setBrush(Qt.NoBrush)
    for i in range(1, 7):
        p.drawEllipse(QPointF(size * 0.5, size * 0.46), size * 0.062 * i, size * 0.062 * i)
    p.setPen(QPen(QColor(255, 255, 255, 70), size * 0.012))
    p.drawLine(QPointF(size * 0.1, size * 0.60), QPointF(size * 0.9, size * 0.60))

    f = QFont("Segoe UI")
    f.setPointSizeF(size * 0.062)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#fdf7e6"))
    p.drawText(QRect(0, int(size * 0.63), size, int(size * 0.13)), Qt.AlignCenter,
               "LITTLE CODE SAUCE")
    f2 = QFont("Segoe UI")
    f2.setPointSizeF(size * 0.036)
    p.setFont(f2)
    p.setPen(QColor(255, 255, 255, 200))
    p.drawText(QRect(0, int(size * 0.76), size, int(size * 0.1)), Qt.AlignCenter,
               "R E T R O   C A S S E T T E")
    p.end()
    return QPixmap.fromImage(img)


def background(w, h, top="#1c2a37", bottom="#0c141b"):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient

    img = QImage(w, h, QImage.Format_ARGB32)
    p = QPainter(img)
    grad = QLinearGradient(0, 0, 0, h)
    grad.setColorAt(0.0, QColor(top))
    grad.setColorAt(1.0, QColor(bottom))
    p.fillRect(img.rect(), grad)
    glow = QRadialGradient(QPointF(w * 0.5, -h * 0.12), w * 0.8)
    glow.setColorAt(0.0, QColor(96, 156, 204, 70))
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(img.rect(), glow)
    p.end()
    return img


def compose(widget, out_name, pad=30):
    from PySide6.QtGui import QPainter

    pm = widget.grab()
    img = background(pm.width() + pad * 2, pm.height() + pad * 2)
    p = QPainter(img)
    p.drawPixmap(pad, pad, pm)
    p.end()
    path = os.path.join(OUT_DIR, out_name)
    img.save(path)
    print(f"[shot] {os.path.relpath(path, ROOT)}  {img.width()}x{img.height()}")


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    import main as m

    os.makedirs(OUT_DIR, exist_ok=True)
    p = m.PlayerApp()

    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    p.model.load_files(paths)
    for t in p.model.tracks:
        folder = os.path.basename(os.path.dirname(t.path))
        p.model.apply_meta(t.path, {"artist": "Little Code Sauce", "album": folder,
                                    "duration": 248.0})
    p.model.set_current(1)

    # 主界面摆成"播放中"的样子
    p._meter_timer.stop()                 # 停掉真实电平轮询，否则它会把表针拉回零
    p.deck.set_cover(demo_cover())
    p.deck.set_labels("Midnight Run", "Little Code Sauce · Side A")
    p.deck.set_playing(True)
    p.deck.set_loop_mode(2)
    p.deck.set_progress(96.0, 248.0)
    p.deck.set_volume(0.72)
    p.deck.set_meters({"vu_l": 0.34, "vu_r": 0.29, "out_peak_l": 0.55, "out_peak_r": 0.5})
    for _ in range(60):                       # 让 VU 指针平滑到位（不依赖定时器）
        p.deck._update_vu(0.04)
    p.deck.repaint()
    compose(p.deck_win, "hero.png")

    # 播放列表
    p.pl_win.rebuild(p.model.tracks, 1)
    for _ in range(6):
        app.processEvents()
    compose(p.pl_win, "playlist.png")

    # console 面板（打开状态，POWER 亮着、输入区开着）
    p.toggle_console()
    for _ in range(8):
        app.processEvents()
    p.console_win.sync_state(p.engine.tape_params(), p.engine.tape_modules(), True)
    p.console_win.repaint()
    compose(p.console_win, "console.png")
    p.console_win.hide()

    # 两套内置皮肤对比
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QPainter

    skins = ["default", "yellow90"]
    crops = []
    for name in skins:
        p.apply_skin(name)
        p.deck.repaint()
        crops.append((name, p.deck.grab(QRect(196, 118, 568, 400))))
    w, h, gap, top = crops[0][1].width(), crops[0][1].height(), 14, 30
    img = background(w * len(crops) + gap * (len(crops) - 1) + 40, h + top + 20)
    pt = QPainter(img)
    f = QFont("Segoe UI")
    f.setPointSizeF(11)
    f.setBold(True)
    pt.setFont(f)
    for i, (name, pm) in enumerate(crops):
        x = 20 + i * (w + gap)
        pt.drawPixmap(x, top, pm)
        pt.setPen(QColor("#cfe0ee"))
        pt.drawText(QRect(x, 4, w, 22), Qt.AlignHCenter | Qt.AlignVCenter, name)
    pt.end()
    img.save(os.path.join(OUT_DIR, "skin-compare.png"))
    print(f"[shot] docs/images/skin-compare.png  {img.width()}x{img.height()}")

    p.engine.stop()
    app.quit()
    print("[shot] 完成")


if __name__ == "__main__":
    main()
