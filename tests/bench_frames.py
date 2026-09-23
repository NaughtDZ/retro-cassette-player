"""帧耗时基准：验证换带动画的两处优化到底省了多少。

- 旧绘制路径：每帧清掉 SVG 光栅缓存 + 重画底座/按钮/标签/磁带（等价优化前的每帧行为）
- 新绘制路径：静态层用缓存位图，只重画磁带
- 换带起步：测主线程调用 engine.pause()（内部会掐解码进程）阻塞了多久

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\bench_frames.py
"""
import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)


def main():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication

    qapp = QApplication(sys.argv[:1])
    import main as m

    # 临时挪走真实会话：否则会恢复上千首歌并在后台逐个 ffprobe，测量与退出都不干净
    # （测完原样放回，不动用户的播放列表）
    session_file = os.path.join(ROOT, ".cache", "session.json")
    saved_session = None
    if os.path.isfile(session_file):
        with open(session_file, "rb") as f:
            saved_session = f.read()
        os.remove(session_file)

        import atexit

        def _restore_session():
            try:
                with open(session_file, "wb") as fh:
                    fh.write(saved_session)
            except OSError:
                pass

        atexit.register(_restore_session)      # 测量中途出错也不丢用户会话

    p = m.PlayerApp()
    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    p.model.load_files(paths)
    for t in p.model.tracks:
        p.model.apply_meta(t.path, {"artist": "YG", "album": "A", "duration": 8.0})
    p._play_index(0)
    p.deck.set_labels("基准测试", "YG · A")
    p.deck.set_progress(3.0, 12.0)
    p.deck.set_playing(True)
    # 定格在换带拔出动画中（位移 + 旋转 + 半透明，最费的一帧）
    p.deck._anim.mode = "ejecting"
    p.deck._anim.t = 0.25
    p.deck._anim.offset_y = -22.0
    p.deck._anim.rot_deg = -4.0
    p.deck._anim.alpha = 0.75

    img = QImage(p.deck.size(), QImage.Format_ARGB32_Premultiplied)
    N = 40

    def timeit(fn, n=N):
        for _ in range(3):
            fn()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t0) / n * 1000.0

    def legacy_frame():
        p.deck.skin._pm_cache.clear()          # 强制每帧重新光栅化 SVG
        img.fill(0)
        pt = QPainter(img)
        pt.setRenderHint(QPainter.Antialiasing)
        p.deck._paint_base(pt)
        for k in p.deck.CTRL_KEYS:
            p.deck._paint_button(pt, k)
        for k in p.deck.CHROME_KEYS:
            p.deck._paint_button(pt, k)
        p.deck._paint_loop_label(pt)
        p.deck._paint_tape(pt)
        pt.end()

    def new_frame():
        img.fill(0)
        p.deck.render(img)

    a = timeit(legacy_frame)
    b = timeit(new_frame)
    print(f"[bench] 旧路径（每帧 SVG + 全量重绘） {a:7.2f} ms/帧  (~{1000.0 / max(a, 0.01):5.1f} fps)")
    print(f"[bench] 新路径（静态层缓存 + 只画磁带） {b:7.2f} ms/帧  (~{1000.0 / max(b, 0.01):5.1f} fps)")
    print(f"[bench] 单帧绘制提速 {a / max(b, 0.001):.2f}×，每帧省下 {a - b:.2f} ms")

    # 换带起步：GUI 线程调 pause() 的阻塞时间
    t0 = time.perf_counter()
    p.deck._anim.mode = "idle"
    p.engine.pause()
    dt_pause = (time.perf_counter() - t0) * 1000.0
    print(f"[bench] 换带起步 engine.pause() 阻塞 GUI {dt_pause:.2f} ms")

    # 动画时长是否与真实时间一致（帧率波动不该拉长动画）
    p.deck._anim.trigger_swap()
    t0 = time.perf_counter()
    guard = 0.0
    while p.deck._anim.mode != "idle" and guard < 3.0:
        p.deck._anim.update(1.0 / 60.0, True)
        guard += 1.0 / 60.0
    print(f"[bench] 换带全流程（60fps 模拟）{guard * 1000:.0f} ms "
          f"(设计值 {(p.deck._anim.EJECT_DUR + p.deck._anim.INSERT_DUR) * 1000:.0f} ms)")

    # 投影缓存：跑一段换带动画，确认没有一帧在重建投影
    p.deck_win.rebuild_shadow()
    assert p.deck_win._shadow is not None, "投影缓存没生成"
    calls = []
    orig_rebuild = p.deck_win.rebuild_shadow

    def counting_rebuild():
        calls.append(1)
        orig_rebuild()

    p.deck_win.rebuild_shadow = counting_rebuild
    p.deck._anim.mode = "ejecting"
    p.deck._anim.t = 0.1
    for _ in range(12):
        p.deck._anim.update(1.0 / 60.0, True)
        img.fill(0)
        p.deck.render(img)
    assert not calls, f"动画帧仍在重建投影（{len(calls)} 次）"
    print("[bench] 投影：12 帧换带动画中重建 0 次（缓存生效，不再每帧整窗模糊）")

    # ---- 播放列表重绘：上千首时，"只画可见行"比"全表逐行排版"快多少 ----
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QRegion
    from app.playlist_model import Track

    sheet = p.pl_win.paper.sheet
    sheet.resize(470, 560)
    sheet.set_rows([Track(os.path.join(ROOT, "tests", "fake", f"t{i:04d}.mp3")) for i in range(2244)], 0)
    img2 = QImage(470, 560, QImage.Format_ARGB32_Premultiplied)

    def visible_only():
        img2.fill(0)
        sheet.render(img2, QPoint(0, 0), QRegion(0, 0, 470, 560))

    def whole_list():
        img2.fill(0)
        sheet.render(img2, QPoint(0, 0), QRegion(0, 0, 470, sheet.height()))

    v_ms = timeit(visible_only)
    w_ms = timeit(whole_list, 2)
    print(f"[bench] 列表重绘（2244 首）可见区 {v_ms:.2f} ms vs 全表 {w_ms:.2f} ms "
          f"→ 快 {w_ms / max(v_ms, 0.001):.0f}×")

    p.engine.stop()
    for w in list(getattr(p, "_cover_workers", [])):
        if w.isRunning():
            w.wait(2000)
    pw = getattr(p, "_probe_worker", None)
    if pw is not None and pw.isRunning():
        pw.wait(3000)
    if saved_session is not None:          # 原样放回用户会话
        with open(session_file, "wb") as f:
            f.write(saved_session)
    qapp.quit()


if __name__ == "__main__":
    import traceback

    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
