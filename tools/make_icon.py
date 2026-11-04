"""生成应用图标 desktop/assets/icon.png 和 icon.ico（字幕框造型）。需要 pillow。"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "desktop" / "assets"


def draw(size: int) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # 背景：圆角方块 + 从靛蓝到紫的渐变
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        c = (int(109 + (139 - 109) * t), int(127 + (92 - 127) * t), int(250 + (246 - 250) * t), 255)
        gd.line([(0, y), (size, y)], fill=c)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(58 * s), fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    # 字幕框
    w = max(2, int(14 * s))
    d.rounded_rectangle([int(40 * s), int(62 * s), int(216 * s), int(194 * s)], radius=int(26 * s), outline="white", width=w)
    # 两行字幕
    for x0, x1, y in [(72, 116, 112), (136, 184, 112), (72, 144, 150), (164, 184, 150)]:
        d.rounded_rectangle([int(x0 * s), int((y - 7) * s), int(x1 * s), int((y + 7) * s)], radius=int(7 * s), fill="white")
    return img


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw(256)
    big.save(OUT / "icon.png")
    big.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成", OUT / "icon.png", OUT / "icon.ico")
