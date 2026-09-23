# third_party/dusk_ctm — 磁带 DSP 核心（来自 dusk-audio-plugins）

这里放的是 [dusk-audio/dusk-audio-plugins](https://github.com/dusk-audio/dusk-audio-plugins)
里 **TapeMachine 的框架无关 DSP 核心**，用来给播放器加"过一遍磁带"的味道。

## 来源与版本

- 上游仓库：`https://github.com/dusk-audio/dusk-audio-plugins`
- 取用 commit：`d7aca75f723f627ba74476fad3f0e6cdc39dfbf5`（2026-09-21）
- 原路径：`plugins/TapeMachine/core/` 与 `plugins/shared-daf/dsp/`

| 本目录文件 | 上游路径 |
| --- | --- |
| `core/TapeMachineDSP.hpp` / `.cpp` | `plugins/TapeMachine/core/` |
| `core/DuskFilters.hpp` | `plugins/shared-daf/dsp/` |
| `core/DuskSVF.hpp` | 同上 |
| `core/DuskSmoothed.hpp` | 同上 |
| `core/DuskOversampler.hpp` | 同上 |
| `core/DuskDenormals.hpp` | 同上 |

这些文件**逐字复制、未做任何修改**，每个文件顶部的版权头原样保留。
`dusk_dsp_shim.cpp` 是我们自己写的 C ABI 包装（不含上游代码），只做接口扁平化、
交错/平面转换与分块调用。

## 许可证：GPL-3.0-or-later

上游与这些文件均为 **GNU GPL v3.0 或更高版本**（全文见 `LICENSE-GPL-3.0.txt`）。
上游外壳依赖的 JUCE 是 GPL/商业双授权，但这里**只取 core，不涉及 JUCE / ImGui / 任何插件外壳**。

对本项目的实际影响：

- **自己用、不对外分发**：GPL 不产生任何义务，随便用。
- **一旦分发**（哪怕免费送出）：链入这些代码的播放器属于衍生作品，整个程序须以 GPL-3.0 发布并提供源码。
  若不想 GPL，可选：把这层换成**独立子进程**（进程边界，接口调用而非链接），
  或者按公开的物理模型（J-A 磁滞、NAB/CCIR 时间常数等公式本身不受版权保护）自己重写实现。
  应用侧的 DSP 调用都被隔离在 `app/tape_fx.py` 一层里，替换形态只改这一层。

## 重新编译

```powershell
# 在项目根目录；需要 Visual Studio 的 C++ 工具链（脚本会自动用 vswhere 找）
.\.venv\Scripts\python.exe tools\build_dusk_dsp.ps1
```

产物：`tools\dusk\dusk_dsp.dll`，由 `app/tape_fx.py` 通过 ctypes 加载。
找不到 DLL 时播放器照常运行，只是没有磁带处理（不会报错、不会静音）。
