"""播放列表持久化自检：M3U8 往返、缺失文件过滤、会话（列表/模式/进度）恢复。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_playlist_io.py
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    import main as m
    from app.playlist_io import load_playlist, load_session, save_m3u, save_session, split_existing

    samples = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    assert len(samples) == 5, f"示例音频应为 5 个，实际 {len(samples)}"

    # 本脚本要往真实会话路径上写测试数据：先把用户会话挪走，结束时原样放回
    session_file = os.path.join(ROOT, ".cache", "session.json")
    saved_session = None
    if os.path.isfile(session_file):
        with open(session_file, "rb") as f:
            saved_session = f.read()
        os.remove(session_file)

    import atexit

    def _restore_user_session():
        try:
            if saved_session is not None:
                with open(session_file, "wb") as fh:
                    fh.write(saved_session)
            elif os.path.isfile(session_file):
                os.remove(session_file)         # 原本就没有会话，别留下测试数据
        except OSError:
            pass

    atexit.register(_restore_user_session)

    p = m.PlayerApp()
    p.model.load_files(samples)
    for t in p.model.tracks:
        folder = os.path.basename(os.path.dirname(t.path))
        p.model.apply_meta(t.path, {"artist": "YG", "album": folder, "duration": 8.0})

    # ---- 1. M3U8 写入 → 读回，顺序与路径必须一致 ----
    # 临时文件放项目目录里（不写系统盘 / 用户目录）
    tmp = os.path.join(ROOT, "tests", "_tmp_playlist.m3u8")
    save_m3u(tmp, p.model.tracks)
    back = load_playlist(tmp)
    assert back == [os.path.abspath(s) for s in samples], f"M3U8 往返不一致：{back}"
    print(f"[io] M3U8 往返 OK：{len(back)} 首")

    # ---- 2. 缺失文件过滤 ----
    ok, missing = split_existing(back + [os.path.join(ROOT, "no_such_file.mp3")])
    assert len(ok) == 5 and len(missing) == 1, f"缺失过滤异常：ok={len(ok)} missing={len(missing)}"
    print(f"[io] 缺失文件过滤 OK：保留 {len(ok)} 首，跳过 {len(missing)} 首")

    # ---- 3. 会话写入 → 新实例启动时自动恢复 ----
    p.model.current = 2
    p.model.set_loop_mode(3)
    save_session(p.session_path, p.model.tracks, 2, 3, 12.5)
    data = load_session(p.session_path)
    assert data and data["current"] == 2 and data["loop_mode"] == 3 and len(data["tracks"]) == 5

    p2 = m.PlayerApp()
    assert len(p2.model.tracks) == 5, f"会话恢复的曲目数不对：{len(p2.model.tracks)}"
    assert p2.model.current == 2, f"当前曲目未恢复：{p2.model.current}"
    assert p2.model.loop_mode == 3, f"播放模式未恢复：{p2.model.loop_mode}"
    assert p2.want_play is False, "恢复会话后不应自动播放"
    print(f"[io] 会话恢复 OK：{len(p2.model.tracks)} 首，当前第 {p2.model.current + 1} 首，"
          f"模式 {p2.model.loop_mode}，且不自动播放")

    os.remove(tmp)
    print("PLAYLIST_IO_PASS")

    # 等后台线程收尾，避免解释器退出时 QThread 仍在运行导致非零退出码
    for owner in (p, p2):
        for w in list(getattr(owner, "_cover_workers", [])):
            if w.isRunning():
                w.wait(3000)
        pw = getattr(owner, "_probe_worker", None)
        if pw is not None and pw.isRunning():
            pw.wait(5000)
    app.quit()


if __name__ == "__main__":
    main()
