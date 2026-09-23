"""冒烟测试 + 截图时间线（offscreen 渲染，无需音卡/显示器）。

验证：皮肤资源装载 / 播放位置推进 / 同专辑切歌 / seek+5s / 跨专辑换带动画 /
track_ended → 自动下一首；并在关键帧保存截图到 tests/shots/。

运行（项目根目录）：.venv\\Scripts\\python.exe app\\smoke.py
"""
import glob
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# offscreen 平台的字体库不会自动扫描系统字体，显式指向系统字体目录（Windows）
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)


def main():
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QWheelEvent
    from PySide6.QtWidgets import QApplication
    qapp = QApplication(sys.argv)

    import main as m
    # 临时挪走会话，保证冒烟从固定状态开始；跑完原样放回，不动用户的播放列表
    # （atexit 兜底：中途断言失败也不会把用户的会话吞掉）
    import atexit
    session_file = os.path.join(ROOT, ".cache", "session.json")
    saved_session = None
    if os.path.isfile(session_file):
        try:
            with open(session_file, "rb") as f:
                saved_session = f.read()
            os.remove(session_file)

            def _restore():
                try:
                    with open(session_file, "wb") as fh:
                        fh.write(saved_session)
                except OSError:
                    pass

            atexit.register(_restore)
        except OSError:
            saved_session = None

    # 配置文件同样临时挪走（冒烟会写它验证持久化）；跑完原样放回
    from app import settings as settings_mod
    cfg_file = settings_mod.config_path(ROOT)
    saved_cfg = None
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "rb") as f:
                saved_cfg = f.read()
            os.remove(cfg_file)

            def _restore_cfg():
                try:
                    with open(cfg_file, "wb") as fh:
                        fh.write(saved_cfg)
                except OSError:
                    pass

            atexit.register(_restore_cfg)
        except OSError:
            saved_cfg = None
    else:
        def _drop_cfg():                    # 原本没有配置文件：跑完把它删掉，别留下测试产物
            try:
                if os.path.isfile(cfg_file):
                    os.remove(cfg_file)
            except OSError:
                pass

        atexit.register(_drop_cfg)
    p = m.PlayerApp()
    p.want_play = True
    p.model.set_loop_mode(0)
    p.deck.set_loop_mode(0)

    shots_dir = os.path.join(ROOT, "tests", "shots")
    os.makedirs(shots_dir, exist_ok=True)

    def pump(sec):
        t0 = time.time()
        while time.time() - t0 < sec:
            qapp.processEvents()
            time.sleep(0.016)

    def shot(name, matte=None):
        """保存截图；matte 指定底色时会额外合成一张不透明预览（便于查看透明窗口效果）。"""
        pm = p.deck.grab()
        pm.save(os.path.join(shots_dir, name))
        if matte:
            canvas = QImage(pm.size(), QImage.Format_ARGB32)
            canvas.fill(QColor(matte))
            pt = QPainter(canvas)
            pt.drawPixmap(0, 0, pm)
            pt.end()
            canvas.save(os.path.join(shots_dir, name.replace(".png", "_preview.png")))
        print(f"[smoke] saved {name}")

    # ---------- 1. 皮肤资源装载检查 ----------
    keys = set(p.deck.skin.assets)
    need = {"base", "tape_shell", "tape_inner", "cover_placeholder"}
    assert need <= keys, f"皮肤资源缺失：{need - keys}"
    print(f"[smoke] 皮肤 'default' 装载完成，共 {len(keys)} 个资源（按钮走程序绘制的塑料键）")

    # ---------- 1b. 主窗口背景透明（除底座/磁带/按钮外 alpha 必须为 0） ----------
    img = p.deck.grab().toImage()
    for x, y, where in ((6, 6, "左上角"), (480, 40, "底座上方"), (900, 200, "右侧空白")):
        a = img.pixelColor(x, y).alpha()
        assert a == 0, f"主窗口背景不透明（{where} alpha={a}）"
    print("[smoke] 背景透明检查通过（空白区 alpha=0）")

    # ---------- 1c. 四种播放模式循环切换 ----------
    seq = []
    for _ in range(4):
        p.on_action("loop")
        seq.append((p.model.loop_mode, p.deck.loop_mode))
    assert seq == [(1, 1), (2, 2), (3, 3), (0, 0)], f"播放模式切换异常：{seq}"
    print("[smoke] 播放模式四态切换 OK（顺序→单曲→列表→随机）")

    # ---------- 2. 载入示例曲目并注入确定性元数据（绕过 ffprobe 异步竞态） ----------
    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    assert len(paths) == 5, f"示例音频应为 5 个，实际 {len(paths)}"
    p.model.load_files(paths)
    for t in p.model.tracks:
        folder = os.path.basename(os.path.dirname(t.path))
        p.model.apply_meta(t.path, {
            "artist": "YG" if folder == "Beta" else "Little Code Sauce",
            "album": folder,
            "duration": 8.0,
        })

    ended_at = []
    p.engine.track_ended.connect(lambda: ended_at.append(time.time()))

    # ---------- 2b. 随机模式：自动下一首不重复当前，人工上一首可回退 ----------
    p.model.set_loop_mode(3)
    p.model.current = 0
    nxt = p.model.auto_next_index()
    assert nxt != 0 and 0 <= nxt < len(p.model.tracks), f"随机下一首异常：{nxt}"
    p.model.current = nxt
    assert p.model.auto_next_index() != nxt, "随机模式连续两次取到同一首"
    assert 0 <= p.model.manual_prev() < len(p.model.tracks), "随机模式上一首异常"
    p.model.set_loop_mode(0)
    p.model.current = -1
    print(f"[smoke] 随机模式 OK（0 -> {nxt}，且不重复）")

    # ---------- 3. 首次上带（插入动画）→ 播放 ----------
    p._play_index(0)
    pump(1.2)
    shot("01_playing.png", matte="#cdd5dd")
    assert p.deck.title_text == "Alpha One", f"标签未设置：{p.deck.title_text!r}"

    pos_a = p.engine.position()
    pump(1.0)
    pos_b = p.engine.position()
    print(f"[smoke] 位置 {pos_a:.2f}s -> {pos_b:.2f}s")
    assert pos_b > pos_a + 0.5, "播放位置未推进"

    # ---------- 3b. 计数器数字要跟着走（位置信号 → 面板刷新） ----------
    shown = p.deck.pos_now
    sec_a = p.deck._shown_sec
    pump(1.2)
    assert p.deck.pos_now > shown + 0.5, \
        f"计数器数字未刷新：{shown:.2f}s -> {p.deck.pos_now:.2f}s"
    assert p.deck._shown_sec != sec_a, "计数器的整秒没有变化，LCD 不会重绘"
    print(f"[smoke] 计数器刷新 OK（{shown:.2f}s -> {p.deck.pos_now:.2f}s，整秒 {sec_a} -> {p.deck._shown_sec}）")

    # ---------- 4. 同专辑下一首（仅快进爆发，不换带） ----------
    p.on_action("next")
    pump(0.5)
    shot("02_next_burst.png")
    assert p.model.current == 1 and p.deck.title_text == "Alpha Two", \
        f"切歌失败：current={p.model.current} title={p.deck.title_text!r}"

    # ---------- 5. seek +5s ----------
    before = p.engine.position()
    p.on_action("seek_fwd")
    pump(0.3)
    after = p.engine.position()
    print(f"[smoke] seek {before:.2f}s -> {after:.2f}s")
    assert after > before + 3, f"seek 未生效：{before:.2f} -> {after:.2f}"

    # ---------- 5b. 切歌后播放键状态必须跟引擎一致 ----------
    assert p.deck.playing == p.engine.playing is True, \
        f"切歌后播放键状态错位：deck={p.deck.playing} engine={p.engine.playing}"
    p.toggle_play()                                    # 暂停
    pump(0.2)
    assert p.deck.playing is False and p.engine.playing is False, "暂停后播放键未切换"
    shot("07_paused.png", matte="#cdd5dd")
    p.toggle_play()                                    # 恢复播放
    pump(0.25)
    assert p.deck.playing is True and p.engine.playing is True, "恢复播放后播放键未切回"
    print("[smoke] 播放/暂停键状态与引擎一致 OK")

    # ---------- 5c. 观察窗进度条拖动跳转 ----------
    tape_r = p.deck.skin.rect("tape")
    prog = p.deck.PROG_RECT
    cy = tape_r.center().y() + prog.center().y()
    x0 = tape_r.center().x() + prog.left() + 3
    x1 = tape_r.center().x() + prog.left() + prog.width() * 0.25

    def send_mouse(kind, x, y):
        ev = QMouseEvent(kind, QPointF(x, y), p.deck.mapToGlobal(QPointF(x, y)).toPoint(),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(p.deck, ev)

    win_before = p.deck_win.pos()
    send_mouse(QEvent.MouseButtonPress, x0, cy)
    send_mouse(QEvent.MouseMove, x1, cy)
    pump(0.15)
    assert p.deck._drag_frac is not None, "按住进度条后没有进入拖动状态"
    assert p.deck._drag_pos is not None and p.deck._drag_pos < p.engine.position(), "拖动预览时间不对"
    shot("08_progress_drag.png", matte="#cdd5dd")
    send_mouse(QEvent.MouseButtonRelease, x1, cy)
    pump(0.35)
    assert p.deck._drag_frac is None, "松手后未退出拖动状态"
    assert p.deck_win.pos() == win_before, "拖进度条时窗口被误拖动"
    target = p.engine.position()
    print(f"[smoke] 进度条拖动跳转 OK（八秒曲目拖到 25% → {target:.2f}s）")
    assert abs(target - 2.0) < 0.7, f"拖动跳转位置不对：{target:.2f}s（期望约 2.0s）"

    # ---------- 5d. 音量旋钮：绕圈拧 + 滚轮 + 与引擎同步 ----------
    knob = p.deck._knob_rect()
    kcx, kcy = knob.center().x(), knob.center().y()
    kring = knob.width() / 2.0 - 6.0
    vol_before = p.deck.volume
    send_mouse(QEvent.MouseButtonPress, kcx, kcy - kring)                 # 从正上方开始拧
    send_mouse(QEvent.MouseMove, kcx - kring * 0.8, kcy + kring * 0.55)   # 往左下拧 → 音量下降
    pump(0.12)
    assert p.deck.volume < vol_before - 0.1, \
        f"拧旋钮没有减小音量：{vol_before:.2f} → {p.deck.volume:.2f}"
    shot("09_volume.png", matte="#cdd5dd")
    send_mouse(QEvent.MouseButtonRelease, kcx - kring * 0.8, kcy + kring * 0.55)
    pump(0.12)
    assert abs(p.engine.volume - p.deck.volume) < 1e-3, \
        f"引擎音量与面板旋钮不同步：engine={p.engine.volume:.3f} deck={p.deck.volume:.3f}"
    print(f"[smoke] 音量旋钮 OK：{vol_before:.2f} → {p.deck.volume:.2f}（引擎已同步）")

    vol_pre_wheel = p.deck.volume
    wheel_ev = QWheelEvent(QPointF(kcx, kcy), p.deck.mapToGlobal(QPointF(kcx, kcy)),
                           QPoint(0, 0), QPoint(0, 120), Qt.NoButton, Qt.NoModifier,
                           Qt.NoScrollPhase, False)
    QApplication.sendEvent(p.deck, wheel_ev)
    pump(0.1)
    assert p.deck.volume > vol_pre_wheel, \
        f"滚轮上调音量失败：{vol_pre_wheel:.2f} → {p.deck.volume:.2f}"
    print(f"[smoke] 滚轮微调音量 OK：{vol_pre_wheel:.2f} → {p.deck.volume:.2f}")

    # ---------- 5e. VU 表：DSP 读数 → 指针位置 的链路 ----------
    pump(0.6)
    diag_m = p.engine.tape_meters()
    floor_db = p.deck.VU_DB_MIN
    norm_l = p.deck._vu_norm(diag_m["vu_l"])
    # 电平在持续变化，两次采样不会完全相等：只要求指针跟着读数走（映射函数另有专门断言）
    assert abs(norm_l - p.deck._vu_target[0]) < 0.15, \
        f"VU 目标值偏离 DSP 读数过多：{norm_l:.4f} vs {p.deck._vu_target[0]:.4f}"
    assert p.deck._vu_norm(0.9) > 0.8, "满幅信号应把指针推到表盘右侧"
    assert p.deck._vu_norm(10 ** (floor_db / 20.0)) < 0.02, "低于表盘下限时应停在最左"
    print(f"[smoke] VU 表链路 OK（DSP vu_l={diag_m['vu_l']:.4f} → 指针 {p.deck._vu_target[0]:.3f}；"
          f"满幅信号 → {p.deck._vu_norm(0.9):.3f}）")
    print(f"[smoke] VU 表跟随 DSP 电平 OK（指针位置 {p.deck._vu_disp[0]:.3f} / "
          f"{p.deck._vu_disp[1]:.3f}）")

    # ---------- 5f. 磁带 console 悬浮窗 ----------
    assert p.engine.tape_available, f"磁带 DSP 不可用：{p.engine.tape_fx.reason}"
    p.toggle_console()
    pump(0.25)
    assert p.console_win.isVisible(), "底座按钮没能呼出 console 面板"
    n_ctrl = len(p.console_win.surface.controls)
    assert n_ctrl >= 18, f"console 控件太少：{n_ctrl}"
    sw = next(c for c in p.console_win.surface.controls if c.name == "oversampling")
    before_os = p.engine.tape_params()["oversampling"]
    p.console_win.surface._bump_switch(sw, +1)          # 点一下挡位旋钮
    pump(0.1)
    assert p.engine.tape_params()["oversampling"] != before_os, \
        "console 挡位旋钮的操作没传到 DSP"
    p.console_win.surface.param_changed.emit("input_gain_db", 9.0)   # 旋钮走一遍回调链
    pump(0.1)
    assert abs(p.engine.tape_params()["input_gain_db"] - 9.0) < 1e-6, "console 旋钮没传到 DSP"
    p.console_win.grab().save(os.path.join(shots_dir, "10_console.png"))
    print(f"[smoke] console 面板 OK（{n_ctrl} 个控件，挡位/旋钮回调均可抵达 DSP）")

    # ---------- 5f-2. 面板空白处必须能拖动窗口（曾因事件不冒泡只能拖 18px 边距） ----------
    surf = p.console_win.surface
    from app.console_window import MARGIN as CM, SURFACE_H, SURFACE_W, TOP_BAR

    blank = None
    for y in range(TOP_BAR + CM + 2, SURFACE_H - CM, 2):
        cand = QPointF(SURFACE_W * 0.55, y)
        if surf._hit(cand) is not None:
            continue
        if any(surf.zone_toggle_rect(i).contains(int(cand.x()), int(cand.y()))
               for i in range(len(surf.zone_rects))):
            continue
        blank = cand
        break
    assert blank is not None, "没找到面板空白处，无法验证拖动"

    def send_to(widget, kind, local):
        gpos = widget.mapToGlobal(local)
        if hasattr(gpos, "toPoint"):
            gpos = gpos.toPoint()
        QApplication.sendEvent(widget, QMouseEvent(kind, local, gpos, Qt.LeftButton,
                                                   Qt.LeftButton, Qt.NoModifier))

    win_before = p.console_win.pos()
    send_to(surf, QEvent.MouseButtonPress, blank)
    send_to(surf, QEvent.MouseMove, QPointF(blank.x() + 40, blank.y() + 26))
    send_to(surf, QEvent.MouseButtonRelease, QPointF(blank.x() + 40, blank.y() + 26))
    pump(0.12)
    win_after = p.console_win.pos()
    assert win_after != win_before, \
        f"面板空白处拖动无效（窗口没动：{win_before} → {win_after}）"
    print(f"[smoke] console 面板可拖动 OK（空白处拖 {win_before.x()},{win_before.y()} → "
          f"{win_after.x()},{win_after.y()}）")
    p.console_win.surface.param_changed.emit("input_gain_db", 0.0)
    p.toggle_console()                                   # 收起
    pump(0.15)
    assert not p.console_win.isVisible(), "console 面板没能收起"

    # ---------- 5g. 效果器分区开关 + 配置持久化 ----------
    p.on_console_param("wow", 42.0)
    p.on_console_param("bias", 0.33)
    p.deck.set_volume(0.42)
    p.console_win.module_toggled.emit("transport", False)      # 一键 bypass 走带分区
    pump(0.1)
    assert p.engine.tape_modules()["transport"] is False, "分区开关没生效"
    tp = p.engine.tape_params()
    assert tp["wow"] == 0.0 and tp["flutter"] == 0.0 and tp["noise"] == 0.0, \
        f"分区关闭没有把参数置中性：wow={tp['wow']} flutter={tp['flutter']} noise={tp['noise']}"
    assert abs(p.engine.export_tape_params()["wow"] - 42.0) < 1e-6, \
        "分区关闭时应记住用户原值，以便开回来"
    print("[smoke] 分区开关 OK（TRANSPORT 关闭 → wow/flutter/noise 置中性，原值被记住）")
    p.console_win.module_toggled.emit("transport", True)       # 开回来
    pump(0.1)
    assert abs(p.engine.tape_params()["wow"] - 42.0) < 1e-6, \
        f"分区开关打开后没有恢复用户值：{p.engine.tape_params()['wow']}"

    p._save_settings()                                   # 写 config\settings.json
    pump(0.1)
    assert os.path.isfile(cfg_file), f"配置没写到 {cfg_file}"
    with open(cfg_file, encoding="utf-8") as f:
        cfg = json.load(f)
    assert abs(float(cfg["volume"]) - 0.42) < 1e-6, f"音量没进配置：{cfg.get('volume')}"
    assert abs(float(cfg["fx"]["params"]["bias"]) - 0.33) < 1e-4, "效果器参数没进配置"
    assert cfg["fx"]["modules"]["transport"] is True, f"分区开关状态没进配置：{cfg['fx']['modules']}"
    assert os.path.dirname(cfg_file).startswith(ROOT), "配置写到了项目目录之外！"
    print(f"[smoke] 配置持久化 OK（{os.path.relpath(cfg_file, ROOT)}，含音量/参数/分区开关，"
          f"且只写项目目录）")

    # ---------- 6. 跨专辑切歌 → 换带（拔出/插入）动画 ----------
    p._play_index(3)                    # Beta One：专辑不同 → 先拔出再插入
    pump(0.28)                          # 拔出一半（EJECT_DUR=0.55s 的中点附近）
    shot("03_eject_mid.png")
    pump(1.7)                           # 插入完成，新带开始播放
    assert p.model.current == 3 and p.deck.title_text == "Beta One", \
        f"换带失败：current={p.model.current} title={p.deck.title_text!r}"
    shot("04_inserted_beta.png")

    # ---------- 7. 练习册式播放列表窗口截图 ----------
    path = os.path.join(shots_dir, "05_playlist.png")
    p.pl_win.grab().save(path)
    print(f"[smoke] saved {path}")

    # ---------- 8. 自然播完 → 自动下一首（同专辑，不再换带） ----------
    t_end = time.time()
    while not ended_at and time.time() - t_end < 9.5:
        qapp.processEvents()
        time.sleep(0.02)
    assert ended_at, "track_ended 未触发"
    print(f"[smoke] track_ended 触发（等待 {time.time() - t_end:.1f}s）")
    pump(1.5)
    assert p.model.current == 4 and p.deck.title_text == "Beta Two", \
        f"自动下一首失败：current={p.model.current} title={p.deck.title_text!r}"
    shot("06_auto_next.png")

    print("[smoke] ALL PASS — tests/shots/ 下已更新截图")
    if saved_session is not None:              # 放回用户会话
        try:
            with open(session_file, "wb") as f:
                f.write(saved_session)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
