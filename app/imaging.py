"""图像处理：解码、页面摆正、双页分割、指纹与缩略图。"""

from __future__ import annotations

import cv2
import numpy as np


# ---------- 编解码 ----------


def decode(buf: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图像解码失败")
    return img


def encode_jpeg(img: np.ndarray, quality: int = 86) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def shrink(img: np.ndarray, max_width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= max_width:
        return img
    scale = max_width / float(w)
    return cv2.resize(img, (max_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)


def thumb(img: np.ndarray, max_width: int = 720, quality: int = 78) -> bytes:
    return encode_jpeg(shrink(img, max_width), quality)


def quarter_turn(img: np.ndarray, turns: int) -> np.ndarray:
    """整幅旋转 90°×turns，正数逆时针。"""
    return np.ascontiguousarray(np.rot90(img, k=turns % 4))


def looks_sideways(boxes, min_boxes: int = 5, ratio: float = 1.2) -> bool:
    """看文字框是「竖着的高条」还是「横着的长条」，判断页面有没有躺倒。

    手机横着拿的时候书页在画面里是躺着的，文字行变成竖方向，OCR 会成片漏掉。
    实测正常横排页面框的中位宽高比约 0.12，躺倒的约 8.1，中间隔了两个数量级，
    所以用中位数判很稳。框太少（画面里没多少字）就不下结论。
    """
    ratios = []
    for box in boxes:
        pts = np.asarray(box, dtype=np.float32)
        width = float(pts[:, 0].max() - pts[:, 0].min())
        height = float(pts[:, 1].max() - pts[:, 1].min())
        if width > 4 and height > 4:
            ratios.append(height / width)
    if len(ratios) < min_boxes:
        return False
    return float(np.median(ratios)) > ratio


def enhance(img: np.ndarray) -> np.ndarray:
    """轻度增强：拉一点局部对比度。不做二值化，OCR 自己会处理。"""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


# ---------- 摆正 ----------


def _order_points(points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def _quad_from_mask(mask: np.ndarray) -> np.ndarray | None:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    h, w = mask.shape[:2]
    best = None
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:4]:
        if cv2.contourArea(contour) < 0.2 * w * h:
            continue
        perimeter = cv2.arcLength(contour, True)
        for eps in (0.02, 0.03, 0.05, 0.08):
            approx = cv2.approxPolyDP(contour, eps * perimeter, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue
            quad = approx.reshape(4, 2).astype(np.float32)
            if best is None or cv2.contourArea(quad) > cv2.contourArea(best):
                best = quad
            break
    return best


def _hugs_border(quad: np.ndarray, w: int, h: int, tol: float = 0.04) -> bool:
    corners = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    gap = np.linalg.norm(_order_points(quad) - corners, axis=1).max()
    return bool(gap < tol * float(np.hypot(w, h)))


def rectify(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """把桌上的书页摆正。找不到可信的四边形就原样返回。

    明暗两种二值化各找一遍：桌面深就取「亮处是纸」那一版，桌面浅就取反。
    候选里挑面积最大的那个**合格**四边形——单纯挑最大会把整幅画面选进去。
    """
    h, w = img.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    kernel = np.ones((9, 9), np.uint8)
    candidates = []
    for flag in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, mask = cv2.threshold(gray, 0, 255, flag + cv2.THRESH_OTSU)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        quad = _quad_from_mask(mask)
        if quad is not None:
            candidates.append(quad)
    if not candidates:
        return img, False

    picked = None
    for quad in candidates:
        area = cv2.contourArea(quad)
        if not 0.25 <= area / float(w * h) <= 0.92:
            continue
        if _hugs_border(quad, w, h):
            continue
        src = _order_points(quad)
        tw = int(max(np.linalg.norm(src[0] - src[1]), np.linalg.norm(src[2] - src[3])))
        th = int(max(np.linalg.norm(src[0] - src[3]), np.linalg.norm(src[1] - src[2])))
        if tw < 240 or th < 240 or not 0.35 <= tw / float(th) <= 1.9:
            continue
        if picked is None or area > cv2.contourArea(picked[0]):
            picked = (quad, tw, th)
    if picked is None:
        return img, False

    quad, tw, th = picked
    dst = np.float32([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]])
    matrix = cv2.getPerspectiveTransform(_order_points(quad), dst)
    return cv2.warpPerspective(img, matrix, (tw, th), flags=cv2.INTER_CUBIC), True


def _fit_line(points):
    """主成分意义下拟合一条直线，返回 (线上一点, 方向)。"""
    pts = np.asarray(points, np.float64)
    center = pts.mean(axis=0)
    axis = np.linalg.svd(pts - center, full_matrices=False)[2][0]
    return center, axis


def _meet(first, second):
    """两条直线的交点。基本平行时返回 None。"""
    (c1, d1), (c2, d2) = first, second
    matrix = np.array([d1, -d2], np.float64).T
    if abs(float(np.linalg.det(matrix))) < 1e-8:
        return None
    t = np.linalg.solve(matrix, c2 - c1)
    return c1 + float(t[0]) * d1


def _pick_line(pool, sequence, need_span: float, axis: int):
    """按给定顺序取若干行拼成一条边，直到这些点在 axis 方向上铺开得够宽。

    固定取「最下面三行」是不行的：行尾短的时候这几个点在横向上挤成一堆，
    拟合出来会是一条竖线。所以按铺开宽度决定取几行。
    """
    picked: list = []
    for index in sequence:
        picked.append(pool[index])
        joined = np.vstack(picked)
        if len(picked) >= 2 and float(np.ptp(joined[:, axis])) >= need_span:
            break
    return _fit_line(np.vstack(picked))


def _valid_quad(quad, shape) -> bool:
    height, width = shape[:2]
    # 四个角都得落在画面里（留 15% 余量）。有一条边拟合歪了，交点就会飞到天边，
    # 这种四边形拉出来是废图，必须拦住，让它退回纸边法。
    margin_x, margin_y = width * 0.15, height * 0.15
    if (
        quad[:, 0].min() < -margin_x
        or quad[:, 0].max() > width + margin_x
        or quad[:, 1].min() < -margin_y
        or quad[:, 1].max() > height + margin_y
    ):
        return False
    if not cv2.isContourConvex(quad.reshape(-1, 1, 2)):
        return False
    return 0.08 <= cv2.contourArea(quad.astype(np.float32)) / float(width * height) <= 0.995


def page_quad_from_text(boxes, shape, min_boxes: int = 8) -> np.ndarray | None:
    """用文字行拟合正文区域的四边形。

    靠纸的轮廓去截页面在现实中经常不成立：白纸摆在浅色桌面上根本分不出边界，
    翻开的书页本身还是弯的，页边还可能被手指压住。但文字一定落在纸上，而且正文块
    在现实里就是个矩形——投影歪成什么样，都能从它的四条边反推回来。

    四条边各用各的样本：上边从最顶上的行往下取、下边从最底下的行往上取、
    左边看所有行的左端、右边**只**看最长的那几行——行尾参差不齐，全拿去拟合
    会把右边界拉进正文里。取到哪一行为止由「这些点在对应方向上有没有铺开」决定。
    """
    if len(boxes) < min_boxes:
        return None

    quads = [np.asarray(b, np.float32).reshape(4, 2) for b in boxes]
    top_corners, bottom_corners, left_corners, right_corners = [], [], [], []
    centers, widths = [], []
    for quad in quads:
        # 检测器给的顺序是「左上→右上→右下→左下」，直接按边取。
        # 千万别按坐标排序去挑上下角：页面被透视拉过之后行本身是斜的，
        # 左边比右边高，按 y 排会把「左下角」当成「右上角」，整条上边就废了。
        if float(np.linalg.norm(quad[1] - quad[0])) < float(np.linalg.norm(quad[3] - quad[0])):
            quad = quad[[0, 3, 2, 1]]          # 框是竖的，先掰成横的
        top_corners.append(quad[[0, 1]])
        bottom_corners.append(quad[[3, 2]])
        left_corners.append(quad[[0, 3]])
        right_corners.append(quad[[1, 2]])
        centers.append(float(quad[:, 1].mean()))
        widths.append(float(quad[:, 0].max() - quad[:, 0].min()))

    points = np.vstack(quads)
    need_x = float(np.ptp(points[:, 0])) * 0.6
    need_y = float(np.ptp(points[:, 1])) * 0.6
    top_to_bottom = np.argsort(centers)
    widest_first = np.argsort(widths)[::-1]

    top = _pick_line(top_corners, top_to_bottom, need_x, 0)
    bottom = _pick_line(bottom_corners, top_to_bottom[::-1], need_x, 0)
    left = _pick_line(left_corners, top_to_bottom, need_y, 1)
    right = _pick_line(right_corners, widest_first, need_y, 1)

    corners = [_meet(top, left), _meet(top, right), _meet(bottom, right), _meet(bottom, left)]
    if all(corner is not None for corner in corners):
        quad = np.array(corners, np.float32)
        if _valid_quad(quad, shape):
            return quad

    # 四条边基本平行（页面本来就平）时交点不存在，退回最小外接矩形：
    # 至少把倾斜和多余背景去掉，比什么都不做强。
    quad = cv2.boxPoints(cv2.minAreaRect(points.astype(np.float32))).astype(np.float32)
    if _valid_quad(quad, shape):
        return quad
    return None


def warp_quad(img: np.ndarray, quad: np.ndarray, pad: float = 0.04) -> np.ndarray | None:
    """把四边形里的内容拉成矩形，四周留一点余量。"""
    height, width = img.shape[:2]
    src = _order_points(quad)
    center = src.mean(axis=0)
    src = center + (src - center) * (1.0 + pad)
    src[:, 0] = np.clip(src[:, 0], 0, width - 1)
    src[:, 1] = np.clip(src[:, 1], 0, height - 1)

    tw = int(max(np.linalg.norm(src[0] - src[1]), np.linalg.norm(src[2] - src[3])))
    th = int(max(np.linalg.norm(src[0] - src[3]), np.linalg.norm(src[1] - src[2])))
    if tw < 200 or th < 200 or not 0.2 <= tw / float(th) <= 5.0:
        return None

    dst = np.float32([[0, 0], [tw - 1, 0], [tw - 1, th - 1], [0, th - 1]])
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, matrix, (tw, th), flags=cv2.INTER_CUBIC)


# ---------- 双页分割 ----------


def find_gutter(ink: np.ndarray) -> int | None:
    """在中间一段里找书脊：那一列的墨迹明显少于正文列，就认它是缝。"""
    h, w = ink.shape[:2]
    column = ink.sum(axis=0).astype(np.float64)
    if column.sum() <= 0:
        return None
    lo, hi = int(w * 0.34), int(w * 0.66)
    if hi - lo < 20:
        return None
    k = max(3, (hi - lo) // 12)
    smooth = np.convolve(column, np.ones(k) / k, mode="same")
    x = lo + int(np.argmin(smooth[lo:hi]))
    # 「有字的列大概有多少墨」：只看真的落过墨的列，页面留白多也不影响
    inked = column[column > 0]
    if inked.size == 0:
        return None
    limit = 0.30 * float(np.median(inked))
    if smooth[x] > limit:
        return None
    if x < w * 0.05 or x > w * 0.95:
        return None
    # 看看这个缝到底占多宽，取中点，切出来的两页留白才匀
    left_edge, right_edge = x, x
    while left_edge > lo and smooth[left_edge - 1] <= limit:
        left_edge -= 1
    while right_edge < hi - 1 and smooth[right_edge + 1] <= limit:
        right_edge += 1
    return (left_edge + right_edge) // 2


def split_spread(
    img: np.ndarray, mode: str = "auto", order: str = "lr"
) -> list[dict]:
    """切成左/右页，返回 [{label, offset, image}]。切不动就整体当一页。"""
    h, w = img.shape[:2]
    pieces: list[dict] = []
    if mode != "off":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        x = find_gutter(ink)
        # auto 模式下，只有画面本身是展开的横版才切
        if x is not None and (mode == "on" or w > h * 0.95):
            left, right = ink[:, :x], ink[:, x:]
            total = ink.sum()
            if total > 0 and left.sum() > 0.04 * total and right.sum() > 0.04 * total:
                pieces = [
                    {"label": "left", "offset": (0, 0), "image": img[:, :x]},
                    {"label": "right", "offset": (x, 0), "image": img[:, x:]},
                ]
    if not pieces:
        pieces = [{"label": "full", "offset": (0, 0), "image": img}]
    if order == "rl" and len(pieces) == 2:
        pieces = [pieces[1], pieces[0]]
    return pieces


# ---------- 指纹 ----------


def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def small_gray(gray: np.ndarray, width: int = 160) -> np.ndarray:
    h, w = gray.shape[:2]
    if w == 0 or h == 0:
        return np.zeros((1, 1), np.float32)
    return cv2.resize(
        gray, (width, max(1, int(h * width / float(w)))), interpolation=cv2.INTER_AREA
    ).astype(np.float32)


def diff(a: np.ndarray, b: np.ndarray) -> float:
    """两帧的平均灰度差，归一到 0~1。"""
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    return float(np.abs(a - b).mean() / 255.0)


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Otsu 分出「有字的地方」。光照整体变化时阈值跟着走，掩码基本不动。"""
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return mask > 0


def mask_distance(a: np.ndarray, b: np.ndarray) -> float:
    """两个墨迹掩码的杰卡德距离：0 表示完全一样，1 表示毫不相干。

    比直接比灰度均值稳得多——一页字多字少、整体偏亮偏暗都不影响判断。
    """
    if a.shape != b.shape:
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    left, right = ink_mask(a), ink_mask(b)
    union = np.count_nonzero(left | right)
    if union == 0:
        return 0.0
    return float(np.count_nonzero(left ^ right)) / float(union)


def dhash(gray: np.ndarray, size: int = 8) -> int:
    """差值哈希，用来判断两张图是不是同一页。"""
    g = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming(a: int, b: int) -> int:
    return int(a ^ b).bit_count()
