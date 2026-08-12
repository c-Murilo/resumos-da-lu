"""Gera os ícones do PWA: um L escuro sobre o verde-limão da marca."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "public" / "icons"
LIME = (215, 255, 100, 255)
INK = (37, 32, 49, 255)


def draw_icon(size, bleed=False, scale=1.0):
    """bleed=True: fundo até a borda (maskable). scale encolhe o L na área segura."""
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if bleed:
        d.rectangle([0, 0, s, s], fill=LIME)
    else:
        d.rounded_rectangle([0, 0, s, s], radius=int(s * 0.22), fill=LIME)

    # L centrado, desenhado como dois retângulos arredondados que se sobrepõem.
    w, h = 128 * scale, 512 * scale          # haste vertical
    foot_w, foot_h = 384 * scale, 128 * scale  # pé horizontal
    cx, cy = s / 2, s / 2
    left = cx - foot_w / 2
    top = cy - h / 2
    r = int(28 * scale)

    d.rounded_rectangle([left, top, left + w, top + h], radius=r, fill=INK)
    d.rounded_rectangle([left, top + h - foot_h, left + foot_w, top + h], radius=r, fill=INK)

    return img.resize((size, size), Image.LANCZOS)


OUT.mkdir(parents=True, exist_ok=True)
draw_icon(192).save(OUT / "icon-192.png")
draw_icon(512).save(OUT / "icon-512.png")
draw_icon(512, bleed=True, scale=0.72).save(OUT / "icon-maskable-512.png")
draw_icon(180).save(OUT / "apple-touch-icon.png")
print("ok:", sorted(p.name for p in OUT.iterdir()))
