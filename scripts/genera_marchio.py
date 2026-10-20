"""Genera il marchio Effigie: testa di profilo sfaccettata (low-poly), icona, logo, splash."""
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy.spatial import Delaunay

OUT = Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(exist_ok=True)
SCRATCH = OUT

# profilo di una testa che guarda a destra, con collo e taglio del busto (griglia 0-1000)
PROFILE = [(300, 930), (300, 800), (330, 700), (300, 610), (250, 520), (235, 420), (260, 310),
           (320, 215), (410, 150), (520, 125), (630, 140), (720, 195), (775, 270), (790, 350),
           (785, 400), (830, 470), (800, 490), (790, 520), (805, 545), (790, 570), (795, 600),
           (770, 640), (700, 650), (660, 700), (640, 800), (680, 930)]
PALETTE = [(30, 90, 190), (45, 120, 225), (60, 145, 240), (90, 170, 250), (130, 195, 255),
           (38, 104, 205)]


def low_poly(size, seed=7):
    """Immagine RGBA della testa sfaccettata."""
    rng = np.random.default_rng(seed)
    poly = np.array(PROFILE, float)
    path = Polygon(poly)
    pts = list(poly)
    while len(pts) < len(poly) + 70:                  # punti interni a caso
        p = rng.uniform([230, 120], [830, 930])
        if path.contains(Point(p)) and min(np.linalg.norm(np.array(pts) - p, axis=1)) > 55:
            pts.append(p)
    pts = np.array(pts)
    tri = Delaunay(pts)
    ss = 4
    k = size * ss / 1000
    img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    light = np.array([-0.6, -0.8])                     # luce da in alto a sinistra
    for simplex in tri.simplices:
        t = pts[simplex]
        c = t.mean(0)
        if not path.contains(Point(c)):
            continue
        rel = (c - [530, 520]) / 400
        shade = 0.55 + 0.45 * float(np.clip(-(rel @ light), -1, 1)) + rng.uniform(-0.12, 0.12)
        i = int(np.clip(shade, 0, 0.999) * len(PALETTE))
        col = sorted(PALETTE, key=sum)[i]
        d.polygon([tuple(q * k) for q in t], fill=col + (255,))
        d.line([tuple(q * k) for q in np.vstack([t, t[:1]])], fill=(160, 205, 255, 70),
               width=max(1, int(1.2 * ss * size / 256)))
    return img.resize((size, size), Image.LANCZOS)


def rounded_bg(size, radius):
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel((0, y), (int(40 - 22 * t), int(42 - 22 * t), int(48 - 24 * t)))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    bg.paste(grad.resize((size, size)), (0, 0), mask)
    return bg, mask


def font(names, size):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


SEMI = ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"]
LIGHT = ["segoeuisl.ttf", "segoeui.ttf", "arial.ttf"]

# ---- icona
S = 1024
bg, mask = rounded_bg(S, 220)
glow = Image.new("L", (S, S), 0)
ImageDraw.Draw(glow).ellipse((200, 150, 860, 860), fill=120)
glow = glow.filter(ImageFilter.GaussianBlur(110))
blue = Image.new("RGBA", (S, S), (55, 142, 240, 255))
bg.paste(blue, (0, 0), Image.fromarray((np.asarray(glow) * (np.asarray(mask) / 255)).astype(np.uint8)))
head = low_poly(760)
bg.alpha_composite(head, (150, 140))
bg.save(OUT / "icona.png")
bg.save(OUT / "icona.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

def title(d, x, y, size, sub, title_fill, sub_fill, gap):
    """Nome e sottotitolo: il sottotitolo va sotto l'ingombro vero del nome (con la
    discendente della g), così non si sovrappongono mai."""
    f = font(SEMI, size)
    d.text((x, y), "Effigie", font=f, fill=title_fill)
    bottom = d.textbbox((x, y), "Effigie", font=f)[3]
    d.text((x + size * 0.04, bottom + gap), "DALLE FOTO ALLA STAMPA 3D", font=font(LIGHT, sub),
           fill=sub_fill)


# ---- logo orizzontale (simbolo + nome) su trasparente: versione per sfondo chiaro
# (logo.png) e per sfondo scuro (logo_chiaro.png)
W, H = 1400, 380
for name, title_fill, sub_fill in (("logo.png", (28, 30, 36, 255), (95, 104, 118, 255)),
                                   ("logo_chiaro.png", (236, 238, 242, 255),
                                    (150, 160, 175, 255))):
    logo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    logo.alpha_composite(low_poly(330), (10, 25))
    title(ImageDraw.Draw(logo), 370, 30, 180, 44, title_fill, sub_fill, 18)
    logo.save(OUT / name)

# ---- splash (sfondo; testo di stato e barra li disegna l'app)
W, H = 760, 420
sp = Image.new("RGB", (W, H), (24, 24, 26))
glow = Image.new("L", (W, H), 0)
ImageDraw.Draw(glow).ellipse((-120, -60, 420, 480), fill=110)
sp.paste((40, 110, 220), (0, 0), glow.filter(ImageFilter.GaussianBlur(120)))
# trama a triangoli leggera sulla destra
d = ImageDraw.Draw(sp, "RGBA")
for x in range(300, W + 120, 60):
    d.line([(x, 0), (x - 240, H)], fill=(255, 255, 255, 10))
    d.line([(x - 240, 0), (x, H)], fill=(255, 255, 255, 10))
sp = sp.convert("RGBA")
sp.alpha_composite(low_poly(300), (30, 40))
d = ImageDraw.Draw(sp)
title(d, 350, 96, 92, 22, (240, 242, 246), (150, 160, 175), 12)
d.rectangle((0, 0, W - 1, H - 1), outline=(60, 62, 68))
sp.convert("RGB").save(OUT / "splash.png")

for name, z in (("icona.png", 260), ("logo.png", 700), ("splash.png", 760)):
    im = Image.open(OUT / name)
    im.thumbnail((z, z))
    canvas = Image.new("RGB", im.size, (60, 60, 60))
    canvas.paste(im, (0, 0), im.convert("RGBA"))
    pass
low_poly(512).save(OUT / "simbolo.png")
print("ok")
