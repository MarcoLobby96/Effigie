#!/usr/bin/env python3
"""
genera_immagini_readme.py — Immagini di docs/ per il README, dai dati veri dei progetti:
il percorso foto → camere → nuvola densa → modello → stampa (Buddha) e la volpe con i colori.

  python scripts/genera_immagini_readme.py <progetto_buddha> <cartella_foto_buddha>
         <foto_stampa> <foto_stampa_dettaglio> [esempio_a_colori]
"""
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import bust_pipeline as bp  # noqa: E402

DOCS = ROOT / "docs"
BG = (30, 31, 34)
W, H = 420, 520


def font(size, bold=False):
    for n in (("seguisb.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    return ImageFont.load_default()


def clean_photo(src, height):
    """Foto senza EXIF (niente GPS né dati del telefono), ridimensionata."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((10_000, height), Image.LANCZOS)
    return Image.frombytes("RGB", im.size, im.tobytes())        # nessun metadato


def best_face_photo(files):
    """La foto (e la rotazione) in cui il volto si vede meglio di fronte (YuNet)."""
    import cv2
    det = cv2.FaceDetectorYN.create(str(ROOT / "tools" / "modelli" / "yunet.onnx"), "",
                                    (320, 320), 0.6)
    best = (-1, None)
    for f in files:
        img = clean_photo(f, 640)
        for k in range(4):
            im = img.rotate(90 * k, expand=True)
            a = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
            det.setInputSize((a.shape[1], a.shape[0]))
            _, faces = det.detect(a)
            if faces is not None and len(faces):
                fc = faces[np.argmax(faces[:, -1])]
                # volto grande, sicuro e con gli occhi alla stessa altezza (dritto)
                score = fc[-1] * fc[2] * fc[3] / (1 + abs(fc[5] - fc[7]))
                if score > best[0]:
                    best = (score, (f, k))
    f, k = best[1] if best[1] else (files[len(files) // 2], 0)
    return clean_photo(f, 900).rotate(90 * k, expand=True)


def fit(img, w=W, h=H):
    return ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.45))


def to_final(points, transform):
    return (np.c_[points, np.ones(len(points))] @ np.asarray(transform).T)[:, :3]


def frame_view(xyz_list, w=W, h=H, margin=36):
    """Scala comune per tutte le viste da davanti (x a destra, z in alto, y profondità)."""
    allp = np.vstack(xyz_list)
    lo, hi = allp[:, [0, 2]].min(0), allp[:, [0, 2]].max(0)
    s = min((w - 2 * margin) / (hi[0] - lo[0]), (h - 2 * margin) / (hi[1] - lo[1]))
    c = (lo + hi) / 2

    def proj(p):
        return np.c_[w / 2 + (p[:, 0] - c[0]) * s, h / 2 - (p[:, 2] - c[1]) * s]
    return proj


def render_points(pts, cols, proj, size=2, w=W, h=H):
    img = Image.new("RGB", (w, h), BG)
    a = np.asarray(img).copy()
    order = np.argsort(-pts[:, 1])                  # da dietro in avanti (davanti = -y)
    xy = proj(pts[order]).astype(int)
    c = cols[order]
    for dx in range(size):
        for dy in range(size):
            x, y = xy[:, 0] + dx, xy[:, 1] + dy
            ok = (x >= 0) & (y >= 0) & (x < w) & (y < h)
            a[y[ok], x[ok]] = c[ok]
    return Image.fromarray(a)


def render_mesh(mesh, proj, colors=None, w=W, h=H):
    v, f = mesh.vertices, mesh.faces
    n = mesh.face_normals
    vis = n[:, 1] < 0.05                            # rivolte verso chi guarda (-y)
    light = np.clip(0.3 + 0.7 * np.clip(n @ np.array([-0.35, -0.75, 0.55]), 0, 1), 0, 1)
    base = np.array([214, 214, 220]) if colors is None else colors
    shade = (base * (0.45 + 0.55 * light[:, None])) if colors is not None else \
        (base * light[:, None])
    xy = proj(v)
    order = np.argsort(-v[f].mean(1)[:, 1])
    order = order[vis[order]]
    img = Image.new("RGB", (w * 2, h * 2), BG)
    d = ImageDraw.Draw(img)
    for i in order:
        d.polygon([tuple(q) for q in xy[f[i]] * 2], fill=tuple(int(x) for x in shade[i]))
    return img.resize((w, h), Image.LANCZOS)


def read_sparse(path):
    pts, cols = [], []
    with open(path, "rb") as fh:
        (n,) = struct.unpack("<Q", fh.read(8))
        for _ in range(n):
            fh.read(8)
            pts.append(struct.unpack("<3d", fh.read(24)))
            cols.append(struct.unpack("<3B", fh.read(3)))
            fh.read(8)
            (t,) = struct.unpack("<Q", fh.read(8))
            fh.seek(8 * t, 1)
    return np.array(pts), np.array(cols)


def label(img, text, step):
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((14, 14, 14 + 28, 14 + 28), 6, fill=(55, 142, 240))
    d.text((28, 28), str(step), font=font(16, True), fill="white", anchor="mm")
    d.text((52, 28), text, font=font(17, True), fill=(235, 236, 240), anchor="lm")
    return img


def strip(panels, path, arrows=True):
    gap = 46 if arrows else 16
    out = Image.new("RGB", (len(panels) * W + (len(panels) - 1) * gap + 40, H + 40), (22, 22, 24))
    d = ImageDraw.Draw(out)
    for k, p in enumerate(panels):
        x = 20 + k * (W + gap)
        mask = Image.new("L", p.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), 14, fill=255)
        out.paste(p, (x, 20), mask)
        if arrows and k < len(panels) - 1:
            ax, ay = x + W + gap / 2, 20 + H / 2
            d.polygon([(ax - 9, ay - 12), (ax + 11, ay), (ax - 9, ay + 12)], fill=(90, 150, 240))
    out.save(path, optimize=True)
    print("creata", path)


def main():
    buddha, photos, stampa, dettaglio = map(Path, sys.argv[1:5])
    volpe = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    DOCS.mkdir(exist_ok=True)
    import trimesh

    # foto delle stampe: senza EXIF, ridotte (le impronte delle dita non restano leggibili)
    clean_photo(stampa, 900).save(DOCS / "stampa_buddha.jpg", quality=86)
    clean_photo(dettaglio, 640).save(DOCS / "stampa_buddha_dettaglio.jpg", quality=84)

    # ---- percorso del Buddha, tutto nella stessa vista da davanti
    info = json.loads((buddha / "busto.json").read_text())
    T = info["transform"]
    mesh = trimesh.load(buddha / "busto.stl")
    sp, sc = read_sparse(buddha / "sparse_aligned" / "points3D.bin")
    sp = to_final(sp, T)
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(buddha / "mvs" / "scene_dense.ply"))
    m = ms.current_mesh()
    dp = to_final(m.vertex_matrix(), T)
    dc = (m.vertex_color_matrix()[:, :3] * 255).astype(np.uint8)
    keep = (dp[:, 2] > mesh.bounds[0, 2] - 2) & (np.abs(dp[:, 0]) < 70) & (np.abs(dp[:, 1]) < 70)
    dp, dc = dp[keep], dc[keep]
    sel = np.random.default_rng(0).choice(len(dp), min(len(dp), 600_000), replace=False)
    dp, dc = dp[sel], dc[sel]
    keep = (np.abs(sp[:, 0]) < 70) & (np.abs(sp[:, 1]) < 70) & (sp[:, 2] > -10)
    sp, sc = sp[keep], sc[keep]
    proj = frame_view([mesh.vertices])

    files = sorted(p for p in photos.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    p1 = label(fit(best_face_photo(files)), "Le tue foto", 1)
    p2 = label(render_points(sp, np.full((len(sp), 3), (120, 175, 255)), proj, 3),
               "Punti chiave (COLMAP)", 2)
    p3 = label(render_points(dp, dc, proj, 2), "Nuvola densa (OpenMVS)", 3)
    p4 = label(render_mesh(mesh, proj), "Modello stampabile", 4)
    p5 = label(fit(clean_photo(stampa, 900)), "Stampato!", 5)
    strip([p1, p2, p3, p4, p5], DOCS / "percorso.png")

    if volpe is None:
        return
    # ---- esempio a colori (facoltativo): foto, forma, colori
    tex = trimesh.load(volpe / "busto.glb", force="mesh")
    tex.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # Y su -> Z su
    tex.apply_scale(1000)
    # sdraiata sul retro: per la foto la rimettiamo in piedi, faccia verso chi guarda
    tex.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    vc = tex.visual.to_color().vertex_colors[:, :3].astype(float)
    fc = vc[tex.faces].mean(1)
    gray = tex.copy()
    projv = frame_view([tex.vertices])
    vphotos = sorted((volpe / "foto").iterdir())
    q1 = label(fit(clean_photo(vphotos[10], 900)), "Foto", 1)
    q2 = label(render_mesh(gray, projv), "Forma (STL / 3MF)", 2)
    q3 = label(render_mesh(tex, projv, fc), "Con i colori (GLB / USDZ)", 3)
    strip([q1, q2, q3], DOCS / "volpe.png")


if __name__ == "__main__":
    main()
