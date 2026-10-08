"""把 OCR 行拼成阅读块，猜区域类型，定阅读顺序。

这里是启发式，不追求「版面分析模型」那种精度，够用就行：
行距和横向重叠决定合不合并，字号和文本形状决定它是标题还是正文。
"""

from __future__ import annotations

import re

import numpy as np

PAGE_NUMBER = re.compile(r"^(page\s*)?[\divxlcdm]{1,7}$", re.I)
CAPTION = re.compile(r"^(fig(ure)?\.?|table|exhibit|chart|box)\s*\d+", re.I)
HEADING = re.compile(
    r"^(chapter|part|section|appendix|prologue|epilogue|"
    r"introduction|conclusion|preface|contents|index|abstract)\b",
    re.I,
)
MATH_CHARS = set("=+−-×÷<>≤≥∑∏∫√≈≠^_{}[]|/\\")


def _bbox(box) -> tuple[float, float, float, float]:
    pts = np.asarray(box, dtype=np.float32)
    return (
        float(pts[:, 0].min()),
        float(pts[:, 1].min()),
        float(pts[:, 0].max()),
        float(pts[:, 1].max()),
    )


def _stitch(parts: list[str]) -> str:
    """把同一段的多行接起来。行尾连字符 + 小写开头，按断词处理。"""
    out = ""
    for part in parts:
        if not out:
            out = part
            continue
        stripped = part.lstrip()
        if out.endswith("-") and stripped[:1].islower():
            out = out[:-1] + stripped
        else:
            out = f"{out} {stripped}"
    return out.strip()


def _is_math(text: str) -> bool:
    if len(text) < 12:
        return False
    hits = sum(1 for ch in text if ch in MATH_CHARS)
    return hits / float(len(text)) > 0.10


def _overlap(a: dict, b: dict) -> float:
    """两行的横向重叠，除以较窄那行的宽度。"""
    left = min(a["box"][2], b["box"][2]) - max(a["box"][0], b["box"][0])
    width = min(a["box"][2] - a["box"][0], b["box"][2] - b["box"][0])
    return left / width if width > 0 else 0.0


def _classify(text: str, height: float, body_height: float, line_count: int) -> str:
    if CAPTION.match(text) and len(text) < 240:
        return "caption"
    if _is_math(text) and len(text) < 400:
        return "formula"
    if HEADING.match(text) and len(text) <= 90:
        return "heading"
    if line_count <= 3 and height >= body_height * 1.22 and len(text) <= 90:
        return "heading"
    if text.isupper() and 8 <= len(text) <= 80 and line_count <= 2:
        return "heading"
    return "paragraph"


def _head_like(row: dict, body_height: float) -> bool:
    return row["height"] >= body_height * 1.18 and len(row["text"]) <= 90


def _median(values: list[float], fallback: float) -> float:
    return float(np.median(values)) if values else fallback


def build_blocks(
    lines: list[dict],
    origin: tuple[int, int] = (0, 0),
    side: str = "full",
) -> list[dict]:
    """lines 来自单页 OCR 结果，坐标是这一页内部的。返回的坐标已加回全图。"""
    if not lines:
        return []

    rows = []
    for line in lines:
        x0, y0, x1, y1 = _bbox(line["box"])
        rows.append(
            {
                "text": line["text"],
                "score": float(line["score"]),
                "box": (x0, y0, x1, y1),
                "height": max(1.0, y1 - y0),
            }
        )
    rows.sort(key=lambda r: r["box"][1])

    heights = np.array([r["height"] for r in rows], dtype=np.float32)
    body_height = float(np.median(heights))
    page_height = max(r["box"][3] for r in rows)

    # 先估行距：同一栏里相邻两行的顶边距离。段落之间的额外间距靠它来区分。
    pitches = [
        b["box"][1] - a["box"][1]
        for a, b in zip(rows, rows[1:])
        if _overlap(a, b) >= 0.3 and b["box"][1] - a["box"][1] > 0
    ]
    pitch = _median(pitches, body_height * 1.4)
    gap_limit = max(pitch * 0.7, body_height * 0.6)

    groups: list[list[dict]] = [[rows[0]]]
    for row in rows[1:]:
        current = groups[-1]
        last = current[-1]
        gap = row["box"][1] - last["box"][3]
        head_row = _head_like(row, body_height)
        head_block = _head_like(current[0], body_height)

        joinable = (
            _overlap(row, last) >= 0.3
            and -body_height <= gap <= gap_limit
            and 0.65 <= row["height"] / max(last["height"], 1e-6) <= 1.55
        )
        # 标题自己成段；标题跟正文之间必须断开
        if head_row != head_block:
            joinable = False
        elif head_row:
            joinable = joinable and abs(row["height"] - current[0]["height"]) <= 0.25 * body_height

        if joinable:
            current.append(row)
        else:
            groups.append([row])

    blocks: list[dict] = []
    ox, oy = origin
    for group in groups:
        rows_ = group
        text = _stitch([r["text"] for r in rows_])
        x0 = min(r["box"][0] for r in rows_)
        y0 = min(r["box"][1] for r in rows_)
        x1 = max(r["box"][2] for r in rows_)
        y1 = max(r["box"][3] for r in rows_)
        height = max(r["height"] for r in rows_)

        # 页眉页脚里孤零零的页码 / 书名，丢掉
        if len(rows_) == 1 and len(text) <= 8 and PAGE_NUMBER.match(text.strip()):
            continue
        if len(rows_) == 1 and len(text) <= 40 and (
            y1 < page_height * 0.045 or y0 > page_height * 0.955
        ):
            continue

        blocks.append(
            {
                "type": _classify(text, height, body_height, len(rows_)),
                "side": side,
                "rect": [x0 + ox, y0 + oy, x1 + ox, y1 + oy],
                "text": text,
                "confidence": round(float(np.mean([r["score"] for r in rows_])), 3),
                "font_height": round(height, 1),
            }
        )

    blocks.sort(key=lambda b: (b["rect"][1], b["rect"][0]))
    return blocks
