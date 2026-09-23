# 皮肤制作指南

一个皮肤包就是 `skins/` 下的一个目录。播放器启动后从菜单 **皮肤** 里切换，也可以改
`config/settings.json` 的 `skin` 字段。

内置两套样板：

| 皮肤 | 特点 | 建议用途 |
| --- | --- | --- |
| `default` | 透明塑料磁带壳 + 蓝色梯形底座，按钮/旋钮由程序绘制 | 改配色、换外壳的起点 |
| `yellow90` | 经典黄黑不透明外壳 + 挖穿的观察窗，继承 `default` 其余部件 | 学"只覆盖一层"的最小写法 |

> 想快速看效果：改完跑 `tests/skin_preview.py`（把所有皮肤并排渲染成对比图），
> 或跑 `tools/make_screenshots.py`（生成整机截图到 `docs/images/`）。两者都是 offscreen 渲染，不用开程序。

---

## 1. 最小皮肤：只改一层

复制 `skins/default` 为 `skins/my-skin`，留一个 `skin.json` 和你想改的资源，其余删掉：

```json
{
  "name": "my-skin",
  "description": "我的磁带外观",
  "base_pack": "default",
  "colors": {
    "base": "#7a4a2b",
    "base_edge": "#3d2413",
    "base_lip": "#c08a55",
    "btn_bg": "#e8c27a"
  },
  "assets": {
    "tape_shell": "tape_shell.svg"
  }
}
```

- `base_pack`：先加载这个包的**配色与资源**，再用本包的覆盖 → 只写要改的部分。
- `assets` 里没列到、磁盘上也没有的资源 → 自动回退到**程序矢量绘制**，不会报错。
- 反过来，想**丢掉继承来的资源、改回程序绘制**，把该键写成空字符串即可：`"base": ""`。

## 2. `skin.json` 字段

### 2.1 colors（配色）

值都是 `#rrggbb` 字符串；拼错的键会被忽略并用默认色（不会崩）。

| 键 | 作用 |
| --- | --- |
| `bg` | 备用背景色（预留） |
| `base` / `base_edge` / `base_lip` | 底座主体 / 描边 / 顶部高光与刻度亮色 |
| `btn_bg` / `btn_hover` / `btn_glyph` | 控制键键帽 / 悬停 / 图标蚀刻色 |
| `chrome_bg` / `chrome_hover` / `chrome_glyph` | 窗口小按钮 |
| `tape_shell` / `tape_edge` / `tape_screw` | 磁带壳（程序兜底绘制时用） |
| `reel_disc` / `reel_hub` / `reel_spoke` | 卷芯盘（程序兜底绘制时用） |
| `text_title` / `text_sub` | 磁带贴纸上的标题 / 副标题 |
| `lcd_bg` / `lcd_fg` | 计数器 LCD 底色 / 数字色 |
| `console_bg_top` `console_bg` `console_bg_bottom` `console_edge` | console 面板的竖直渐变与描边 |
| `console_zone` / `console_zone_line` / `console_label` | console 分区凹槽 / 分隔线 / 文字 |
| `knob_face` / `knob_rim` / `knob_pointer` / `knob_mark` | console 旋钮：键帽 / 外圈 / 刻度亮线 / 指针 |
| `switch_face` | console 挡位旋钮帽 |

### 2.2 layout（几何）

值是 `[x, y, w, h]`（窗口坐标，窗口固定 960×620）。只写要改的，没写的用默认值。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `tape` | `[220, 142, 520, 356]` | 磁带区（内部分层坐标以这里为原点） |
| `base` | `[76, 468, 808, 132]` | 底座梯形 |
| `btn_prev` `btn_play` `btn_next` `btn_stop` `btn_loop` | `[238/312/386/460/560, 516, 46, 46]` | 控制键 |
| `btn_console` | `[610, 516, 46, 46]` | 呼出 console 的键 |
| `knob` | `[134, 508, 64, 64]` | 音量旋钮（正方形区域，按中心/半径绘制） |
| `vu_left` / `vu_right` | `[46, 296, 148, 96]` / `[766, 296, 148, 96]` | 两只 VU 表 |
| `win_min` `win_menu` `win_list` `win_close` | `[662/704/746/788, 520, 34, 34]` | 窗口按钮 |

### 2.3 assets（资源）

键 → 文件名（相对皮肤目录，支持子目录如 `icons/btn_play.svg`）。SVG 与 PNG 都行。

| 资源键 | 画布尺寸 | 说明 |
| --- | --- | --- |
| `base` | 808×132 | 底座整图（会拉伸到 `layout.base`） |
| `tape_inner` | 520×356 | **磁带内部**：观察窗承座、卷盘凹槽、导带柱 |
| `tape_shell` | 520×356 | **磁带外壳**，画在内部之上；透明/半透明即可透出内部 |
| `reel_hub` | 正方形 | 可选：卷芯齿轮盘贴图（程序会按转动角度旋转） |
| `cover_placeholder` | 132×132 | 无封面时的占位图 |
| `console_panel` | 480×686 | 可选：console 面板整块底图 |
| `btn_*` / `win_*` | 46×46 / 34×34 | 可选：按钮整图；不给就由程序画成复古塑料键帽 |

**没有列在这里的部件一律由程序绘制**（VU 表、音量旋钮、进度条、磁带卷与卷芯、console 的每个旋钮/挡位旋钮/开关）——
它们跟随 `colors` 变化，不需要你画资源。

## 3. 磁带的分层与坐标

磁带区在窗口中的位置由 `layout.tape` 决定；资源文件的画布固定 **520×356**，局部坐标系
**原点在磁带中心**，也就是说：

- 画布坐标 = 局部坐标 + (260, 178)
- 观察窗：局部 `x -156..156, y 44..140` → 画布 `x 104..416, y 222..318`
- 贴纸（程序绘制，白纸 + 标题）：局部 `y -152..38` → 画布 `y 26..216`
- 走带进度条：局部 `x -64..64, y 121..129`（程序绘制）

绘制顺序：**内部 → 外壳 → 贴纸(封面/标题) → 计数器**。所以外壳是"盖"在内部上的：

- 做**透明塑料壳**：外壳用半透明填充，内部卷盘自然会透出来（见 `default/tape_shell.svg`）。
- 做**不透明壳**：必须在观察窗处**挖穿**，否则看不到卷盘转动：

```svg
<path fill-rule="evenodd" fill="#2f3336" stroke="#15181a" stroke-width="3"
      d="M 18 2.5 H 502 A 15.5 15.5 0 0 1 517.5 18 V 338 A 15.5 15.5 0 0 1 502 353.5
         H 18 A 15.5 15.5 0 0 1 2.5 338 V 18 A 15.5 15.5 0 0 1 18 2.5 Z
         M 113 231 H 407 A 9 9 0 0 1 416 240 V 300 A 9 9 0 0 1 407 309
         H 113 A 9 9 0 0 1 104 300 V 240 A 9 9 0 0 1 113 231 Z"/>
```

要点：**外框路径 + 窗口路径放在同一个 `path` 里，用 `fill-rule="evenodd"`**，窗口那块就会被挖空。
`yellow90/tape_shell.svg` 是完整可抄的例子。

## 4. 常见坑

| 现象 | 原因 / 处理 |
| --- | --- |
| 换了外壳但看不到卷盘 | 外壳不透明且没挖洞 → 按上节用 `evenodd` 挖穿观察窗 |
| 资源不生效 | 文件名/扩展名写错 → 启动时会打印 `[skin] 资源缺失：…`，看控制台或日志 |
| 颜色改了没用 | 键名拼错（静默回退默认色）；或在子皮肤里被 `base_pack` 的顺序盖掉 |
| 贴图边缘发糊 | SVG 会被**拉伸**到目标矩形，画布比例要与资源表一致（如磁带必须 520×356） |
| 位图有白底 | PNG 必须带透明通道，否则会盖住底下的东西 |
| 按钮/旋钮想换成自己的图 | 按资源键给整图；注意它会**整个替换**键帽（含按下反馈），程序不再画 |
| console 面板想整块换皮 | 给 `console_panel`（480×686）；控件仍会画在它上面，所以底图里要留好控件位置 |

## 5. 调试与预览

```powershell
# 所有皮肤并排对比（输出 tests\shots\10_skin_compare.png）
.\.venv\Scripts\python.exe tests\skin_preview.py

# 生成整机截图（输出 docs\images\hero.png 等）
.\.venv\Scripts\python.exe tools\make_screenshots.py

# 检查资源是否都被加载（会打印皮肤装载条数与缺失项）
.\.venv\Scripts\python.exe app\smoke.py
```

`smooth` 提示：程序对所有 SVG 做了**光栅缓存**（首次按目标尺寸渲染成位图，之后直接贴图），
所以换皮肤后想验证清晰度，直接看截图即可，不用担心运行时缩放损耗。

## 6. 参考实现

- 透明塑料壳 + 程序绘制按钮：[skins/default](../skins/default)
- 不透明黄黑壳 + 挖穿观察窗 + 继承其余部件：[skins/yellow90](../skins/yellow90)
- 磁带内部结构（承座/导带柱）：[skins/default/tape_inner.svg](../skins/default/tape_inner.svg)
