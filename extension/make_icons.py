"""Generate the extension's icons - a white heart on Flipper orange.

Run once; the PNGs are committed so building the package needs no Pillow:

    python extension/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"
SIZES = (16, 32, 48, 96, 128)
ORANGE = (255, 130, 0, 255)
WHITE = (255, 255, 255, 255)


def heart_icon(size, supersample=8):
    big = size * supersample
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # The badge: rounded square, so the icon reads on light and dark toolbars.
    draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=big // 5, fill=ORANGE)

    # Two lobes and a point: at icon sizes this reads as a heart far better than
    # any attempt at the real curve.
    cx, cy = big * 0.5, big * 0.44
    lobe = big * 0.195
    draw.ellipse([cx - 2 * lobe, cy - lobe, cx + lobe * 0.15, cy + lobe], fill=WHITE)
    draw.ellipse([cx - lobe * 0.15, cy - lobe, cx + 2 * lobe, cy + lobe], fill=WHITE)
    # Bridge the seam between the lobes, otherwise a thin slit runs down the
    # middle of the heart at small sizes.
    draw.rectangle([cx - lobe * 0.25, cy - lobe * 0.75,
                    cx + lobe * 0.25, cy + lobe * 0.9], fill=WHITE)
    draw.polygon(
        [(cx - 1.96 * lobe, cy + lobe * 0.5),
         (cx + 1.96 * lobe, cy + lobe * 0.5),
         (cx, cy + 2.4 * lobe)],
        fill=WHITE,
    )
    return image.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon-{size}.png"
        heart_icon(size).save(path)
        print(f"{path.name:14} {path.stat().st_size:6} bytes")
    # The store listing and the README both want one big one.
    heart_icon(512).save(OUT / "icon-512.png")


if __name__ == "__main__":
    main()
