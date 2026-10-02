"""One-off icon generator (not a project dependency).

Usage (from repo root):
    uv run --with pillow python launchers/make_icons.py

Outputs:
    launchers/icon_1024.png
    launchers/mac/AppIcon.icns        (needs macOS `iconutil`)
    launchers/windows/sensor_monitor.ico
and copies the .icns into "Sensor Monitor.app/Contents/Resources/" if the bundle exists.
"""
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SIZE = 1024
SS = 2  # supersampling factor for smooth edges

BG_TOP = (21, 26, 33)      # --surface #151a21
BG_BOT = (14, 17, 22)      # --bg #0e1116
BORDER = (50, 59, 71)      # --border-strong
ACCENT = (76, 141, 255)    # --accent #4c8dff
GRID = (37, 44, 54)        # --border


def stamp(draw, pts, width, fill):
    """Thick smooth stroke: stamp filled circles along densely sampled points."""
    r = width / 2
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def render(size=SIZE):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # rounded square with vertical gradient (macOS-style margin ~ 8%)
    m = int(s * 0.08)
    radius = int((s - 2 * m) * 0.225)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / (s - 1)
        c = tuple(int(BG_TOP[i] * (1 - t) + BG_BOT[i] * t) for i in range(3)) + (255,)
        gd.line([(0, y), (s, y)], fill=c)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([m, m, s - m, s - m], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([m, m, s - m, s - m], radius=radius, outline=BORDER, width=max(2, s // 256))

    # faint grid lines
    x0, x1 = m + (s - 2 * m) * 0.14, s - m - (s - 2 * m) * 0.14
    cy = s / 2
    for k in (-2, -1, 0, 1, 2):
        y = cy + k * (s - 2 * m) * 0.14
        d.line([(x0, y), (x1, y)], fill=GRID, width=max(2, s // 340))

    # waveform: flat -> pulse burst -> flat, amplitude-modulated sine
    amp_max = (s - 2 * m) * 0.27
    pts = []
    n = 4000
    for i in range(n + 1):
        t = i / n
        env = math.exp(-((t - 0.5) / 0.17) ** 2)
        y = cy - amp_max * env * math.sin(2 * math.pi * 4.5 * (t - 0.5) + math.pi / 2 * 0)
        pts.append((x0 + (x1 - x0) * t, y))
    w = max(6, int(s * 0.04))
    # glow
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    stamp(ImageDraw.Draw(glow), pts, int(w * 2.4), ACCENT + (70,))
    from PIL import ImageFilter
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.012))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    stamp(d, pts, w, ACCENT + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main():
    master = render(SIZE)
    master.save(HERE / "icon_1024.png")

    # .ico
    (HERE / "windows").mkdir(exist_ok=True)
    master.save(HERE / "windows" / "sensor_monitor.ico",
                sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])

    # .icns via iconutil
    (HERE / "mac").mkdir(exist_ok=True)
    if shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as td:
            iconset = Path(td) / "AppIcon.iconset"
            iconset.mkdir()
            for base in (16, 32, 128, 256, 512):
                master.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
                master.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "mac" / "AppIcon.icns")],
                           check=True)
        res = ROOT / "Sensor Monitor.app" / "Contents" / "Resources"
        if res.is_dir():
            shutil.copy(HERE / "mac" / "AppIcon.icns", res / "AppIcon.icns")
    else:
        print("iconutil not found; skipped .icns", file=sys.stderr)
    print("done")


if __name__ == "__main__":
    main()
