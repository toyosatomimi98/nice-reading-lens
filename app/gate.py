"""翻页检测状态机。

只吃手机回传的低分辨率预览帧，判断「画面变了且新画面稳住了」。
真正昂贵的 OCR 和翻译由它放行之后才启动。
"""

from __future__ import annotations

import time
import threading

import numpy as np

from . import imaging


class PageGate:
    def __init__(self, get_settings):
        """get_settings 是个无参可调用对象，返回当前 Settings。"""
        self.settings = get_settings
        self._lock = threading.Lock()
        self.base: np.ndarray | None = None
        self.prev: np.ndarray | None = None
        self.stable = 0
        self.locked_until = 0.0

    def reset(self) -> None:
        self.base = None
        self.prev = None
        self.stable = 0
        self.locked_until = 0.0

    def push(self, gray: np.ndarray) -> str:
        """返回 idle / candidate / confirm / cooldown。"""
        with self._lock:
            return self._push(gray)

    def _push(self, gray: np.ndarray) -> str:
        s = self.settings()
        now = time.time()
        frame = imaging.small_gray(gray, 160)

        if self.base is None:
            self.base, self.prev = frame, frame
            return "idle"

        if now < self.locked_until:
            self.prev = frame
            return "cooldown"

        if imaging.mask_distance(frame, self.base) < s.change_jaccard:
            # 还是原来那一页，基准帧不用动：墨迹掩码本身就不受整体明暗影响
            self.prev = frame
            self.stable = 0
            return "idle"

        moved = imaging.mask_distance(frame, self.prev) >= s.stable_jaccard
        self.prev = frame
        self.stable = 0 if moved else self.stable + 1

        if self.stable < s.stable_frames:
            return "candidate"

        self.base = frame
        self.stable = 0
        self.locked_until = now + s.cooldown_ms / 1000.0
        return "confirm"
