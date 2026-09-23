"""磁带动画状态机：滚轴旋转物理 + 快进/快退惯性爆发 + 拔出/插入换带动画。

几何约定（与 deck_widget 的默认布局一致）：
- 左滚轴半径随播放进度增大（收带盘），右滚轴减小（放带盘）。
- v>0 = 正转（播放/快进方向），v<0 = 反转（倒带方向）。
"""
import math

from PySide6.QtCore import QObject, Signal


def clamp01(x):
    return max(0.0, min(1.0, x))


class TapeAnimator(QObject):
    swap_requested = Signal()   # 拔出完成、新带即将插入的瞬间（app 在此换曲并起播）

    EJECT_DUR = 0.55           # 秒：磁带向上滑出并淡出
    INSERT_DUR = 0.65          # 秒：新磁带从上方落回（带回弹过冲）

    HUB_R = 26.0              # px，齿轮盘（卷芯）半径：实物磁带里盘径固定不变
    COIL_MAX = 46.0           # px，满盘时的磁带卷外径
    COIL_MIN = 30.0           # px，空盘时的磁带卷外径（贴着卷芯一圈薄带）
    K0 = 118.0                # px/s，v=1 时的带速基准

    def __init__(self, parent=None):
        super().__init__(parent)
        self.progress = 0.0
        self.left_angle = 0.0     # rad
        self.right_angle = 0.0
        self.burst_v = 0.0        # 额外线速度（倍率），指数衰减
        self.burst_dir = 1.0      # +1 快进 / -1 倒带
        self.mode = "idle"        # idle | ejecting | inserting
        self.t = 0.0
        self.alpha = 1.0
        self.offset_y = 0.0       # px，相对槽位的垂直偏移（负=向上拔出）
        self.rot_deg = 0.0
        self._swapped = False

    # ---------------- 滚轴几何 ----------------
    def left_coil(self):
        """左盘（收带盘）磁带卷外径：随进度增大。"""
        return self.COIL_MIN + (self.COIL_MAX - self.COIL_MIN) * clamp01(self.progress)

    def right_coil(self):
        """右盘（放带盘）磁带卷外径：随进度减小。"""
        return self.COIL_MAX - (self.COIL_MAX - self.COIL_MIN) * clamp01(self.progress)

    # 兼容旧调用名：卷外径即参与转速换算的等效半径
    left_radius = left_coil
    right_radius = right_coil

    # ---------------- 控制 ----------------
    def seek_burst(self, direction, magnitude=3.5):
        """direction: +1 快进 / -1 倒带；magnitude 为相对正常带速的峰值倍率。"""
        if abs(direction) > 1e-6:
            self.burst_v = max(self.burst_v, float(magnitude))
            self.burst_dir = 1.0 if direction > 0 else -1.0

    def trigger_swap(self):
        """触发"拔出→插入"换带动画（仅 idle 时生效）。"""
        if self.mode == "idle":
            self.mode = "ejecting"
            self.t = 0.0
            self._swapped = False

    @property
    def swapping(self):
        return self.mode != "idle"

    # ---------------- 每帧推进 ----------------
    def update(self, dt, playing):
        if self.burst_v > 0.01:
            self.burst_v *= math.exp(-3.2 * dt)
        else:
            self.burst_v = 0.0

        v = (1.0 if playing else 0.0) + self.burst_v * self.burst_dir
        v = max(-8.0, min(8.0, v))
        rl = max(self.left_radius(), 1e-3)
        rr = max(self.right_radius(), 1e-3)
        # ω = K0·v / r：小半径盘转得更快，方向随带速符号翻转
        self.left_angle += (self.K0 * v / rl) * dt
        self.right_angle += (self.K0 * v / rr) * dt

        if self.mode == "ejecting":
            self.t += dt
            k = min(self.t / self.EJECT_DUR, 1.0)
            e = k * k                      # ease-in
            self.alpha = 1.0 - e
            self.offset_y = -34.0 * e      # 向上滑出槽位
            self.rot_deg = -6.0 * e
            if k >= 1.0 and not self._swapped:
                self._swapped = True
                self.mode = "inserting"
                self.t = 0.0
                self.swap_requested.emit()
        elif self.mode == "inserting":
            self.t += dt
            k = min(self.t / self.INSERT_DUR, 1.0)
            b = self._ease_out_back(k)     # 带回弹过冲
            self.alpha = min(1.0, k * 2.4)
            self.offset_y = -34.0 * (1.0 - b)
            self.rot_deg = -6.0 * (1.0 - b)
            if k >= 1.0:
                self.mode = "idle"
                self.t = 0.0
                self.alpha = 1.0
                self.offset_y = 0.0
                self.rot_deg = 0.0

    @staticmethod
    def _ease_out_back(k):
        c1 = 1.70158
        c3 = c1 + 1.0
        return 1.0 + c3 * (k - 1.0) ** 3 + c1 * (k - 1.0) ** 2
