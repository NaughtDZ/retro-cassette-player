"""会话恢复自检：旧格式兼容、元数据快照回填、按需探测（启动不再对上千首全量 ffprobe）。

用法（项目根目录）：.venv\\Scripts\\python.exe tests\\verify_session.py
"""
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
sys.path.insert(0, ROOT)


def drain(app_obj):
    for w in list(getattr(app_obj, "_cover_workers", [])):
        if w.isRunning():
            w.wait(2000)
    pw = getattr(app_obj, "_probe_worker", None)
    if pw is not None and pw.isRunning():
        pw.wait(5000)


def main():
    from PySide6.QtWidgets import QApplication

    qapp = QApplication(sys.argv[:1])
    import main as m
    from app import settings as settings_mod
    from app.playlist_io import save_session

    # 配置文件也要保护：本脚本会写它来验证持久化，跑完原样放回
    cfg_file = settings_mod.config_path(ROOT)
    saved_cfg = None
    if os.path.isfile(cfg_file):
        with open(cfg_file, "rb") as f:
            saved_cfg = f.read()
        os.remove(cfg_file)      # 必须删掉：否则会读到上次遗留的模块旁通状态，测出来的"参数"是被接管后的值

    import atexit

    def _restore_cfg():
        try:
            if saved_cfg is not None:
                with open(cfg_file, "wb") as fh:
                    fh.write(saved_cfg)
            elif os.path.isfile(cfg_file):
                os.remove(cfg_file)
        except OSError:
            pass

    atexit.register(_restore_cfg)

    # ---- 先造一份可重复的测试会话（不依赖用户的真实播放列表）----
    paths = sorted(glob.glob(os.path.join(ROOT, "tests", "samples", "*", "*.mp3")))
    assert len(paths) == 5, f"示例音频应为 5 个，实际 {len(paths)}"
    seed = m.PlayerApp()
    seed.model.load_files(paths)
    for t in seed.model.tracks:
        folder = os.path.basename(os.path.dirname(t.path))
        seed.model.apply_meta(t.path, {"artist": "YG", "album": folder, "duration": 8.0})
    save_session(seed.session_path, seed.model.tracks, 2, 3, 3.5)   # 5 首样本，停在 3.5s
    seed.engine.stop()
    drain(seed)
    print("[v] 已写入测试会话：5 首，当前第 3 首，进度 3.5s，模式 3")

    t0 = time.perf_counter()
    p = m.PlayerApp()
    boot = time.perf_counter() - t0
    n = len(p.model.tracks)
    print(f"[v] 启动恢复耗时 {boot:.2f}s，恢复 {n} 首")

    if n == 0:
        print("[v] 会话为空，跳过后续检查")
        qapp.quit()
        return

    p._save_session()
    with open(p.session_path, encoding="utf-8") as f:
        data = json.load(f)
    tr = data.get("tracks") or []
    with_meta = sum(1 for e in tr if isinstance(e, dict) and e.get("duration"))
    assert data.get("version") == 2, f"会话未升级到 v2：{data.get('version')}"
    assert len(tr) == n, f"保存条目数与列表不一致：{len(tr)} vs {n}"
    print(f"[v] 已保存 v2 会话：{len(tr)} 条，其中 {with_meta} 条带元数据快照")

    # 音量与效果器参数属于"软件配置"，写在 config\settings.json（项目目录内），不再进会话
    p.deck.set_volume(0.35)
    p.on_console_param("bias", 0.21)
    tp_now = p.engine.tape_params()
    assert all(tp_now), f"参数表异常：{tp_now}"
    p._save_settings()
    with open(cfg_file, encoding="utf-8") as f:
        cfg = json.load(f)
    assert abs(float(cfg["volume"]) - 0.35) < 0.01, f"音量未写入配置：{cfg.get('volume')}"
    saved_bias = float(cfg["fx"]["params"].get("bias", -999.0))
    assert abs(saved_bias - 0.21) < 0.01, f"效果器参数未写入配置：bias={saved_bias}（期望 0.21）"
    assert os.path.dirname(cfg_file).startswith(ROOT), f"配置写到了项目目录之外：{cfg_file}"
    assert "volume" not in data, "音量应改由配置文件保存，不该再留在会话里"
    print(f"[v] 配置持久化 OK（{os.path.relpath(cfg_file, ROOT)}：volume={cfg['volume']}, "
          f"fx.params.bias={cfg['fx']['params']['bias']}）")

    # 用明确的"当前曲目 + 进度"重写会话，验证关掉程序后能续上
    p.model.current = 2
    save_session(p.session_path, p.model.tracks, 2, 3, 3.5)   # 样本曲长 8s

    limit = getattr(m.PlayerApp, "SESSION_PROBE_LIMIT", 60)
    if with_meta == 0:
        print(f"[v] 旧格式会话（无快照）→ 首次只补探测 {limit} 首，符合预期")
    else:
        print(f"[v] 快照已回填 {with_meta} 首；其余按需探测（上限 {limit} 首/次）")

    t0 = time.perf_counter()
    p2 = m.PlayerApp()
    boot2 = time.perf_counter() - t0
    assert len(p2.model.tracks) == n, f"二次启动曲目数不一致：{len(p2.model.tracks)} vs {n}"
    assert abs(p2.deck.volume - 0.35) < 0.01, f"音量未随配置恢复：{p2.deck.volume}"
    assert abs(p2.engine.volume - 0.35) < 0.01, f"恢复音量没同步到引擎：{p2.engine.volume}"
    assert abs(p2.engine.tape_params()["bias"] - 0.21) < 0.01, "效果器参数未随配置恢复"
    print(f"[v] 二次启动 {boot2:.2f}s，曲目 {len(p2.model.tracks)} 首（快照回填，无需全量探测）")
    print(f"[v] 音量与效果器参数随配置恢复 OK（volume={p2.deck.volume:.2f}, "
          f"bias={p2.engine.tape_params()['bias']:.2f}）")

    # ---- 关闭程序后：曲目与进度要能续上（预载但不发声，按播放键从原处继续）----
    cur_track = p2.model.tracks[p2.model.current]
    assert p2.engine.path is not None, "恢复会话后没有预载当前曲目"
    assert os.path.abspath(p2.engine.path) == os.path.abspath(cur_track.path), \
        f"预载的不是当前曲目：{p2.engine.path} vs {cur_track.path}"
    assert p2.engine.playing is False, "预载不应自动出声"
    assert abs(p2.engine.position() - 3.5) < 0.6, \
        f"没有定位到上次进度：{p2.engine.position():.2f}s（期望约 3.5s）"
    print(f"[v] 曲目与进度已续上 OK（{os.path.basename(cur_track.path)} @ "
          f"{p2.engine.position():.2f}s，未自动播放）")

    p2.toggle_play()                                     # 按播放键 → 从原处继续，而不是从头/第 0 首
    t0 = time.time()
    while time.time() - t0 < 1.2:
        qapp.processEvents()
        time.sleep(0.016)
    assert p2.engine.playing, "按下播放后没有开始播放"
    assert p2.engine.position() >= 3.4, \
        f"继续播放的位置不对：{p2.engine.position():.2f}s（应从 3.5s 附近继续）"
    assert p2.model.current == 2, "继续播放时跳到了别的曲目"
    print(f"[v] 继续播放 OK（从 {p2.engine.position():.2f}s 起，未跳回第 0 首）")
    p2.engine.stop()

    p.engine.stop()
    p2.engine.stop()
    drain(p)
    drain(p2)
    qapp.quit()
    print("SESSION_PASS")


if __name__ == "__main__":
    import traceback

    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
