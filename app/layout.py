"""把 OCR 行拼成阅读块，猜区域类型，定阅读顺序。

这里是启发式，不追求「版面分析模型」那种精度，够用就行：
行距和横向重叠决定合不合并，字号和文本形状决定它是标题还是正文。
"""

from __future__ import annotations

import re

import numpy as np

PAGE_NUMBER = re.compile(r"^(page\s*)?[\divxlcdm]{1,7}$", re.I)
BARE_NUMBER = re.compile(r"^[\s\[\(（]*\d{1,4}[\s\]\)）]*$")
CAPTION = re.compile(r"^(fig(ure)?\.?|table|exhibit|chart|box)\s*\d+", re.I)
HEADING = re.compile(
    r"^(chapter|part|section|appendix|prologue|epilogue|"
    r"introduction|conclusion|preface|contents|index|abstract)\b",
    re.I,
)
MATH_CHARS = set("=+−-×÷<>≤≥∑∏∫√≈≠^_{}[]|/\\")

# 最常见的英文小词。方向选错时识别出来的是一串糊在一起的怪字符串，
# 命中率会塌下来，所以拿它当「这个方向对不对」的判据。
COMMON_WORDS = frozenset(
    """the of and to in a is that it for was as with his her he she be on at by this had not
    are but from or have an they which one you were all their we when your can said there use
    each about if how will up out them then many some so these would other into has more two
    like him see time could no make than first been its who now people my over did down only
    way find long any new work part take get place made live where after back little round man
    year came show every good me give our under name very through just form much great think
    say help low line before turn cause same mean differ move right boy old too does tell
    sentence set three want air well also play small end put home read hand port large spell
    add even land here must big high such follow act why ask men change went light kind off
    need house picture try us again animal point mother world near build self earth father head""".split()
)

WORD = re.compile(r"[A-Za-z']+")


def word_score(texts) -> float:
    """识别结果里常见英文小词占的比例。

    页面方向转错时，识别出来的词会被糊成一串（"sticksleast letsnightun" 这种），
    命中率明显偏低。用它来在两个旋转方向里挑一个，比看置信度靠谱——置信度对
    糊掉的字照样给 0.95。
    """
    words = [w for w in WORD.findall(" ".join(texts).lower()) if len(w) > 1]
    if len(words) < 20:  # 样本太少不下结论
        return 0.0
    return sum(1 for w in words if w in COMMON_WORDS) / float(len(words))


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


def order_score(texts: list[str]) -> int:
    """一串按顺序排好的行，读起来有多像正常排版。

    小写开头说明上一句还没完，是正常排版里最常见的衔接；上一行没结束却大写
    开头就有点可疑。单靠它区分正反并不可靠（实测只差一两分），所以只当页码
    那条判据失效时的参考。
    """
    score = 0
    for prev, cur in zip(texts, texts[1:]):
        p, c = prev.strip(), cur.strip()
        if not p or not c:
            continue
        if c[0].islower():
            score += 1
        elif p[-1] not in '.!?"”':
            score -= 1
    return score


def looks_flipped(lines: list[dict]) -> bool:
    """整页倒 180° 了没有。

    倒过来时每行的文字本身是对的——引擎的角度分类器会逐行纠正 180°——但行的
    先后顺序整个反了，于是译文从最后一句开始。这是最难判的一种情况：文字层面
    完全看不出问题。

    最可靠的方位标是页码：书里几乎每页都有，而且只会压在正文下方。页码跑到
    正文上方去了，页面就是倒的。没有页码的页面（章节首页、整页插图）退回看
    句子衔接，而且要求差距明显才翻——翻错的代价比不翻大得多。
    """
    if len(lines) < 6:
        return False

    boxes = [np.asarray(line["box"], np.float32) for line in lines]
    tops = np.array([box[:, 1].min() for box in boxes], np.float64)
    lefts = np.array([box[:, 0].min() for box in boxes], np.float64)
    rights = np.array([box[:, 0].max() for box in boxes], np.float64)
    span = float(tops.max() - tops.min())
    width = float(np.percentile(rights, 80) - np.percentile(lefts, 20))
    if span <= 0 or width <= 0:
        return False

    for line, box, top in zip(lines, boxes, tops):
        if not BARE_NUMBER.match(line["text"].strip()):
            continue
        if float(box[:, 0].max() - box[:, 0].min()) > 0.18 * width:
            continue  # 太宽了，大概是正文里的数字，不是页码
        where = (float(top) - float(tops.min())) / span
        if where < 0.12:
            return True  # 页码在最上面 → 页面倒着
        if where > 0.88:
            return False  # 页码在最下面 → 正常

    order = np.argsort(tops)
    texts = [lines[int(i)]["text"] for i in order]
    if word_score(texts) < 0.15:
        return False  # 识别结果本身就不可信，别拿它当依据
    forward = order_score(texts)
    backward = order_score(list(reversed(texts)))
    return backward > forward


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
