"""生成打包用的图标。

仓库里不存二进制，图标每次构建现画一张，省得为了改个颜色去开画图软件。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
PAPER = (245, 242, 236, 255)
INK = (150, 96, 44, 255)
WHITE = (255, 255, 255, 255)


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([6, 6, SIZE - 6, SIZE - 6], radius=48, fill=PAPER, outline=INK, width=9)

    # 摊开的书：中间是书脊，两页向左右展开
    left = [(44, 74), (126, 96), (126, 190), (44, 168)]
    right = [(212, 74), (130, 96), (130, 190), (212, 168)]
    for page in (left, right):
        d.polygon(page, fill=WHITE, outline=INK)
    # PIL 的 polygon 描边偏细，再补一圈粗的
    d.line(left + [left[0]], fill=INK, width=7, joint="curve")
    d.line(right + [right[0]], fill=INK, width=7, joint="curve")
    d.line([(128, 96), (128, 190)], fill=INK, width=7)

    # 底下一条箭头，表示「翻过去就变成中文」
    d.line([(78, 214), (172, 214)], fill=INK, width=9)
    d.polygon([(196, 214), (162, 196), (162, 232)], fill=INK)
    return img


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "icon.ico"
    build().save(out, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"图标已生成：{out}")


if __name__ == "__main__":
    main()
