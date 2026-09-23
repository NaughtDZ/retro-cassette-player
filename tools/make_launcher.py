"""生成程序图标（多尺寸 .ico）并创建无控制台窗口的启动快捷方式。

为什么要这个：双击 .bat 必然先弹出一个 cmd 窗口（哪怕里面用 pythonw 启动），
既闪一下也不优雅。快捷方式直接指向 pythonw.exe，双击无任何控制台窗口，
还能自己带图标、固定到任务栏/开始菜单。

用法（项目根目录）：
    .venv\\Scripts\\python.exe tools\\make_launcher.py            # 项目根 + 桌面各建一个
    .venv\\Scripts\\python.exe tools\\make_launcher.py --no-desktop  # 只在项目根建
"""
import os
import struct
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
SHORTCUT_NAME = "复古磁带播放器.lnk"


def draw_icon(size):
    """画一枚磁带图标（矢量绘制，任意尺寸都锐利）。"""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath,
                               QPen)

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    k = size / 256.0

    def R(x, y, w, h):
        return QRectF(x * k, y * k, w * k, h * k)

    # 磁带壳
    body = QPainterPath()
    body.addRoundedRect(R(16, 40, 224, 176), 18 * k, 18 * k)
    g = QLinearGradient(0, 40 * k, 0, 216 * k)
    g.setColorAt(0.0, QColor("#3f86ad"))
    g.setColorAt(0.55, QColor("#2a6485"))
    g.setColorAt(1.0, QColor("#17405a"))
    p.setPen(QPen(QColor("#0e2b3d"), max(1.0, 6 * k)))
    p.setBrush(QBrush(g))
    p.drawPath(body)

    # 标签纸与两条印线
    lab = QPainterPath()
    lab.addRoundedRect(R(36, 56, 184, 92), 8 * k, 8 * k)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#f8f4e9"))
    p.drawPath(lab)
    p.setPen(QPen(QColor("#c9c0ac"), max(1.0, 2 * k)))
    p.drawLine(QPointF(48 * k, 82 * k), QPointF(208 * k, 82 * k))
    p.drawLine(QPointF(48 * k, 106 * k), QPointF(208 * k, 106 * k))

    # 观察窗 + 磁带 + 两个卷盘
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#12181d"))
    p.drawRoundedRect(R(56, 120, 144, 56), 8 * k, 8 * k)
    p.setBrush(QColor("#4e3b2c"))
    p.drawRect(R(96, 142, 64, 12))
    for cx in (92, 164):
        p.setBrush(QColor("#e8eff3"))
        p.drawEllipse(QPointF(cx * k, 148 * k), 20 * k, 20 * k)
        p.setBrush(QColor("#42505b"))
        p.drawEllipse(QPointF(cx * k, 148 * k), 6.8 * k, 6.8 * k)

    # 底部定位槽 + 两颗螺丝
    p.setBrush(QColor("#0d2536"))
    p.drawRoundedRect(R(92, 192, 72, 9), 4 * k, 4 * k)
    p.setBrush(QColor("#a8bcc8"))
    p.drawEllipse(QPointF(34 * k, 196 * k), 7 * k, 7 * k)
    p.drawEllipse(QPointF(222 * k, 196 * k), 7 * k, 7 * k)
    p.end()
    return img


def write_ico(path, sizes=ICON_SIZES):
    """把多个尺寸的 PNG 打包成 ICO（Vista+ 支持 PNG 负载，小尺寸缩放不糊）。"""
    from PySide6.QtCore import QBuffer, QIODevice

    payloads = []
    for sz in sizes:
        im = draw_icon(sz)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        im.save(buf, "PNG")
        payloads.append((sz, bytes(buf.data())))
        buf.close()

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    entries, blob = b"", b""
    for sz, data in payloads:
        wh = 0 if sz >= 256 else sz          # 0 表示 256
        entries += struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blob += data
    with open(path, "wb") as f:
        f.write(header + entries + blob)
    return path


def make_shortcut(lnk_path, pythonw, main_py, ico, workdir):
    ps = f"""
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut('{lnk_path}')
$lnk.TargetPath = '{pythonw}'
$lnk.Arguments = '"{main_py}"'
$lnk.WorkingDirectory = '{workdir}'
$lnk.IconLocation = '{ico}'
$lnk.Description = '复古磁带播放器'
$lnk.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   check=True, capture_output=True)


def desktop_dir():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out if os.path.isdir(out) else ""
    except Exception:
        return ""


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])          # 只为 Qt 图形栈初始化
    ico = write_ico(os.path.join(ROOT, "icon.ico"))
    print(f"[launcher] 图标已生成：{ico}（{len(ICON_SIZES)} 个尺寸）")

    pythonw = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(pythonw):
        print("[launcher] 未找到 .venv\\Scripts\\pythonw.exe，跳过快捷方式创建")
        app.quit()
        return
    main_py = os.path.join(ROOT, "main.py")

    targets = [os.path.join(ROOT, SHORTCUT_NAME)]
    if "--no-desktop" not in sys.argv:
        d = desktop_dir()
        if d:
            targets.append(os.path.join(d, SHORTCUT_NAME))

    for t in targets:
        make_shortcut(t, pythonw, main_py, ico, ROOT)
        print(f"[launcher] 快捷方式已创建：{t}")
    print("[launcher] 完成：以后双击这个快捷方式即可，不会再闪 cmd 窗口")
    app.quit()


if __name__ == "__main__":
    main()
