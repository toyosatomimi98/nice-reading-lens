"""OCR 封装。默认用 RapidOCR（PaddleOCR 的 ONNX 发行版），按需加载模型。"""

from __future__ import annotations

import threading

import numpy as np


class OcrEngine:
    _init_lock = threading.Lock()

    def __init__(self):
        self._engine = None

    def _ensure(self):
        if self._engine is None:
            with self._init_lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    self._engine = RapidOCR()
        return self._engine

    def warmup(self) -> None:
        self._ensure()

    def detect(self, img: np.ndarray) -> list:
        """只跑文本检测，不跑识别。用来判断页面方向，比整条流程便宜得多。"""
        boxes, _ = self._ensure().text_det(img)
        if boxes is None:
            return []
        return [np.asarray(box, dtype=np.float32) for box in boxes]

    def read(self, img: np.ndarray, min_confidence: float = 0.0) -> list[dict]:
        """返回 [{box, text, score, height}]，box 为四点多边形，顺序即引擎给出的顺序。"""
        result, _ = self._ensure()(img)
        lines: list[dict] = []
        for item in result or []:
            box, text, score = item[0], item[1], float(item[2])
            text = (text or "").strip()
            if not text or score < min_confidence:
                continue
            points = np.asarray(box, dtype=np.float32)
            lines.append(
                {
                    "box": points.astype(float).tolist(),
                    "text": text,
                    "score": score,
                    "height": float(points[:, 1].max() - points[:, 1].min()),
                }
            )
        return lines
