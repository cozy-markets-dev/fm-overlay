"""Makes the loading screen's picture (mprice_decoder/assets/splash.png, drawn by render_splash.cjs) all or nothing
at its rounded edge: each pixel fully see-through or fully solid. Both loading screens show it on a colour Windows
makes see-through (splash.py, splash_resource.py), and a half see-through pixel would be blended with that colour
(a pink rim) or, without it, the corners show filled.
    python3 tools/splash_edges.py
"""
from pathlib import Path

from PIL import Image

PICTURE = Path(__file__).resolve().parents[1] / "mprice_decoder" / "assets" / "splash.png"


def main():
    image = Image.open(PICTURE).convert("RGBA")
    alpha = image.getchannel("A").point(lambda a: 255 if a >= 128 else 0)
    image.putalpha(alpha)
    image.save(PICTURE)
    print(f"{PICTURE}: edge made all or nothing")


if __name__ == "__main__":
    main()
