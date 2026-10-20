"""
extras.py — Funzioni aggiuntive della pipeline:
  - controllo delle foto prima di COLMAP (mosse, buie/bruciate, poco sovrapposte)
  - maschere del soggetto (rembg) per togliere lo sfondo dalla nuvola densa
  - texture vera dalle foto (TextureMesh di OpenMVS) ed export GLB / USDZ
  - spessore minimo per la stampa
  - scala reale da un marker ArUco di dimensione nota
"""

import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import bust_pipeline as bp


# ----------------------------------------------------------------- controllo delle foto
def _stats(path):
    """(nitidezza, luminosità media, frazione di pixel bruciati o neri, immagine piccola)."""
    from PIL import Image
    with Image.open(path) as im:
        im.draft("L", (640, 640))
        g = np.asarray(im.convert("L"), np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    # luce valutata al centro, dove sta il soggetto: il cielo bianco ai bordi è normale
    h, w = g.shape
    c = g[h // 5:h - h // 5, w // 5:w - w // 5]
    clipped = float(np.mean((c >= 250) | (c <= 5)))
    return float(lap.var()), float(c.mean()), clipped, g.astype(np.uint8)


def _matches(a, b, orb, matcher):
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return 0
    pairs = matcher.knnMatch(da, db, k=2)
    return sum(1 for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance)


def rate_photos(files, sample=60):
    """Voto da 1 a 10 delle foto per la fotogrammetria, prima di cominciare, con i motivi.
    Guarda un campione (al massimo `sample` foto, ridotte): numero, risoluzione,
    nitidezza, esposizione, sovrapposizione fra foto vicine e quanti punti riconoscibili
    hanno (superfici lucide, nere o tutte uguali ne hanno pochi)."""
    files = list(files)
    n = len(files)
    if n == 0:
        return {"voto": 1, "motivi": ["nessuna foto"]}
    pick = [files[round(i * (n - 1) / max(min(n, sample) - 1, 1))] for i in range(min(n, sample))]
    from PIL import Image
    sizes = []
    for p in pick[:10]:
        try:
            with Image.open(p) as im:
                sizes.append(min(im.size))
        except OSError:
            pass
    with ThreadPoolExecutor(max_workers=8) as pool:
        stats = list(pool.map(_stats, pick))
    sharp = np.array([s[0] for s in stats])
    med = float(np.median(sharp))
    blurry = float(np.mean(sharp < 0.3 * med))
    badlight = float(np.mean([m < 35 or m > 225 or c > 0.35 for _, m, c, _ in stats]))
    weak, points = 0.0, None
    try:
        import cv2
        orb = cv2.ORB_create(1500)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        gray = [s[3] for s in stats]
        points = float(np.median([len(orb.detect(g, None)) for g in gray]))
        if len(gray) > 2:
            # ogni foto con le due seguenti: i set fatti a più giri non sono in sequenza
            # perfetta; il campione salta foto quando sono tante, quindi soglia più bassa
            thr = 25 if n <= sample else 12
            best = [max(_matches(gray[i], gray[j], orb, matcher)
                        for j in (i + 1, i + 2) if j < len(gray)) for i in range(len(gray) - 1)]
            weak = float(np.mean(np.array(best) < thr))
    except ImportError:
        pass
    score, why = 10.0, []

    def minus(points_off, reason):
        nonlocal score
        if points_off > 0.05:
            score -= points_off
            why.append((points_off, reason))
    minus(4 if n < 10 else 2.5 if n < 20 else 1 if n < 40 else 0,
          f"solo {n} foto (meglio 50-100)")
    side = min(sizes) if sizes else 0
    minus(2.5 if side < 800 else 1.5 if side < 1100 else 0.5 if side < 1500 else 0,
          f"risoluzione bassa ({side} px sul lato corto)")
    minus(min(3.0, 8 * blurry), f"{blurry:.0%} delle foto mosse o sfocate")
    minus(min(3.0, 6 * badlight), f"{badlight:.0%} delle foto troppo buie o bruciate")
    minus(min(2.5, 5 * max(0.0, weak - 0.15)), f"{weak:.0%} delle foto poco sovrapposte alle vicine")
    if points is not None:
        minus(3 if points < 150 else 1.5 if points < 400 else 0.5 if points < 700 else 0,
              "pochi dettagli da agganciare (superficie lucida, scura o uniforme?)")
    why.sort(key=lambda w: -w[0])
    return {"voto": int(np.clip(round(score), 1, 10)), "motivi": [r for _, r in why],
            "foto": n}


def check_photos(img_dir, work):
    """Scarta le foto mosse o con esposizione sbagliata (solo se ne restano abbastanza) e
    segnala quelle che si sovrappongono poco con le vicine. Scrive qualita.json."""
    files = sorted(p for p in Path(img_dir).rglob("*") if p.suffix.lower() in bp.IMAGE_EXTS)
    if len(files) < 10:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        stats = list(pool.map(_stats, files))
    sharp = np.array([s[0] for s in stats])
    med = float(np.median(sharp))
    bad = {}
    for f, (s, mean, clip, _) in zip(files, stats):
        if s < 0.3 * med:
            bad[f] = "mossa/sfocata"
        elif mean < 35:
            bad[f] = "troppo buia"
        elif mean > 225 or clip > 0.35:
            bad[f] = "sovraesposta"
    # sovrapposizione con le foto vicine (in ordine di ripresa)
    weak = []
    try:
        import cv2
        orb = cv2.ORB_create(1500)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        gray = [s[3] for s in stats]
        links = [_matches(gray[i], gray[i + 1], orb, matcher) for i in range(len(files) - 1)]
        for i, f in enumerate(files):
            near = [links[j] for j in (i - 1, i) if 0 <= j < len(links)]
            if max(near) < 25:
                weak.append(f.name)
    except ImportError:
        pass
    removed = []
    if bad and len(files) - len(bad) >= 20 and len(bad) <= 0.3 * len(files):
        trash = Path(work) / "scartate"
        shutil.rmtree(trash, ignore_errors=True)
        trash.mkdir()
        for f, why in bad.items():
            shutil.move(str(f), trash / f.name)
            removed.append(f.name)
        print(f"Scartate {len(removed)} foto ({', '.join(sorted(set(bad.values())))}): "
              f"sono in {trash}")
    elif bad:
        bp.warn(f"{len(bad)} foto mosse o con luce sbagliata: le tengo perché senza "
                "resterebbero troppo poche.")
    if len(weak) > 0.2 * len(files):
        bp.warn(f"{len(weak)} foto hanno poco in comune con le vicine: scatta più foto, "
                "spostandoti meno tra uno scatto e l'altro.")
    try:
        rating = rate_photos(files)
        print(f"Voto delle foto: {rating['voto']}/10"
              + (f" ({'; '.join(rating['motivi'])})" if rating["motivi"] else ""))
    except Exception as e:
        print("Voto delle foto non riuscito:", e)
        rating = None
    (Path(work) / "qualita.json").write_text(json.dumps(
        {"voto": rating, "foto": len(files), "scartate": {f.name: w for f, w in bad.items()
                                          if f.name in removed},
         "dubbie": {f.name: w for f, w in bad.items() if f.name not in removed},
         "poco_sovrapposte": weak}, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------- maschere del soggetto
def make_masks(work):
    """Maschera del soggetto (rembg) per ogni foto raddrizzata da COLMAP, in dense/masks."""
    img_dir, out = Path(work) / "dense" / "images", Path(work) / "dense" / "masks"
    files = sorted(p for p in img_dir.rglob("*") if p.suffix.lower() in bp.IMAGE_EXTS)
    if not files:
        return None
    try:
        from PIL import Image
        from rembg import new_session, remove
        session = new_session("isnet-general-use")
    except Exception as e:
        bp.warn(f"Maschere non disponibili ({e}): tengo lo sfondo.")
        return None
    print("Separo il soggetto dallo sfondo nelle foto...")
    for k, f in enumerate(files, 1):
        dst = out / f.relative_to(img_dir).with_suffix(".png")
        if not dst.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(f) as im:
                small = im.convert("RGB")
                small.thumbnail((1024, 1024))
                mask = remove(small, session=session, only_mask=True)
                mask.resize(im.size, Image.BILINEAR).save(dst)
        bp.PROGRESS.update(0.05 + 0.1 * k / len(files))
    return out


def apply_masks(points, normals, work):
    """Tiene i punti che cadono sul soggetto nella maggior parte delle foto che li vedono."""
    from PIL import Image
    model = Path(work) / "dense" / "sparse"
    masks = Path(work) / "dense" / "masks"
    intr = bp.read_intrinsics(model)
    votes = np.zeros(len(points))
    seen = np.zeros(len(points))
    for name, C, D, U, Rt in bp.read_cameras(model / "images.bin"):
        mpath = (masks / name).with_suffix(".png")
        if not mpath.is_file() or not intr.get(name):
            continue
        fx, fy, cx, cy = intr[name]
        m = np.asarray(Image.open(mpath).convert("L")) > 127
        rel = points - C
        z = rel @ D
        x = np.round((rel @ Rt) / np.maximum(z, 1e-9) * fx + cx).astype(int)
        y = np.round((rel @ -U) / np.maximum(z, 1e-9) * fy + cy).astype(int)
        ok = (z > 0) & (x >= 0) & (y >= 0) & (x < m.shape[1]) & (y < m.shape[0])
        seen[ok] += 1
        votes[ok] += m[y[ok], x[ok]]
    keep = (seen == 0) | (votes >= 0.5 * seen)
    if keep.sum() < 0.05 * len(points):
        bp.warn("Le maschere toglierebbero quasi tutto: le ignoro.")
        return points, normals
    print(f"Maschere: tolti {int((~keep).sum())} punti di sfondo.")
    return points[keep], normals[keep]


# ----------------------------------------------------------------- scala dal marker
def marker_scale(work, marker_mm):
    """mm per unità della ricostruzione da un marker ArUco (dizionario 4x4) nelle foto."""
    try:
        import cv2
    except ImportError:
        return None
    model = Path(work) / "dense" / "sparse"
    if not (model / "images.bin").is_file():
        return None
    intr = bp.read_intrinsics(model)
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    rays = {}
    for name, C, D, U, Rt in bp.read_cameras(model / "images.bin"):
        img = cv2.imread(str(model.parent / "images" / name), cv2.IMREAD_GRAYSCALE)
        if img is None or not intr.get(name):
            continue
        corners, ids, _ = det.detectMarkers(img)
        if ids is None:
            continue
        fx, fy, cx, cy = intr[name]
        for c, i in zip(corners, ids.ravel()):
            for k, (x, y) in enumerate(c[0]):
                d = (x - cx) / fx * Rt - (y - cy) / fy * U + D
                rays.setdefault((int(i), k), []).append((C, d / np.linalg.norm(d)))
    sides = []
    for mid in {i for i, _ in rays}:
        pts = [bp.triangulate(rays[(mid, k)]) if len(rays.get((mid, k), [])) >= 2 else None
               for k in range(4)]
        if all(p is not None for p in pts):
            sides += [np.linalg.norm(pts[k] - pts[(k + 1) % 4]) for k in range(4)]
    if not sides:
        bp.warn("Marker ArUco non trovato in almeno 2 foto: uso l'altezza indicata.")
        return None
    scale = marker_mm / float(np.median(sides))
    print(f"Marker ArUco trovato: scala reale ({scale:.3g} mm per unità).")
    return scale


# ----------------------------------------------------------------- stampa
def thickness_report(mesh, min_mm=1.2, samples=4000):
    """Frazione della superficie più sottile di min_mm. Per ogni punto campione cerca, entro
    min_mm, un punto della superficie opposta (normale rivolta dall'altra parte e dietro):
    con un albero dei vertici è centinaia di volte più veloce che lanciare raggi."""
    if not mesh.is_watertight:
        return None
    from scipy.spatial import cKDTree
    pts, fid = mesh.sample(samples, return_index=True)
    nrm = mesh.face_normals[fid]
    v, vn = mesh.vertices, mesh.vertex_normals
    tree = cKDTree(v)
    thin_flags = np.zeros(samples, bool)
    depth = np.full(samples, np.inf)
    for k, idx in enumerate(tree.query_ball_point(pts, min_mm)):
        if not idx:
            continue
        rel = v[idx] - pts[k]
        behind = rel @ nrm[k] < -0.05                       # dentro il materiale
        opposite = vn[idx] @ nrm[k] < -0.3                  # superficie dall'altra parte
        hit = behind & opposite
        if hit.any():
            thin_flags[k] = True
            depth[k] = float(np.min(-(rel[hit] @ nrm[k])))
    thin = float(np.mean(thin_flags))
    smallest = np.min(depth) if np.isfinite(depth).any() else None
    print(f"Spessore: sotto {min_mm} mm il {thin:.1%} della superficie"
          + (f" (minimo {smallest:.2f} mm)" if smallest is not None else ""))
    if thin > 0.02:
        bp.warn(f"Il {thin:.0%} della superficie è più sottile di {min_mm} mm (orecchie, "
                "punte): ingrandisci il modello o stampa con ugello fine.")
    return thin


# ----------------------------------------------------------------- impostazioni di stampa
A1_BED = 256.0                      # Bambu Lab A1: piatto 256 x 256 x 256 mm, ugello 0.4


def print_settings(mesh, thin=None):
    """Impostazioni per la Bambu Lab A1 decise dalla forma del modello (mm, Z in su)."""
    ext = mesh.extents
    height = float(ext[2])
    area = mesh.area_faces
    n = mesh.face_normals
    zc = mesh.triangles_center[:, 2]
    # sporgenze: facce rivolte verso il basso oltre 45 gradi, non sulla base
    over = (n[:, 2] < -0.707) & (zc > 0.4)
    overhang_mm2 = float(area[over].sum())
    # appoggio: facce piatte sul fondo
    base = (n[:, 2] < -0.99) & (zc < 0.2)
    contact = float(area[base].sum())
    reasons = {}
    s = {"sparse_infill_pattern": "gyroid", "wall_loops": "3", "top_shell_layers": "5",
         "seam_position": "back", "ironing_type": "no ironing"}
    # strato: più fine sui modelli piccoli (dettagli del volto), più spesso sui grandi
    lh = 0.12 if height < 70 else 0.16 if height < 160 else 0.2
    s["layer_height"] = f"{lh:.2f}"
    reasons["layer_height"] = f"altezza {height:.0f} mm"
    # riempimento: i busti sono decorativi, poco riempimento basta (meno tempo e filo)
    vol = abs(float(mesh.volume)) / 1000
    s["sparse_infill_density"] = "8%" if vol > 300 else "12%" if vol > 60 else "15%"
    reasons["sparse_infill_density"] = f"volume {vol:.0f} cm³"
    if thin is not None and thin > 0.02:
        s["wall_loops"] = "4"                       # parti sottili: tutte pareti, niente vuoti
        reasons["wall_loops"] = f"{thin:.0%} della superficie sotto 1,2 mm"
    if overhang_mm2 > 50:
        s.update({"enable_support": "1", "support_type": "tree(auto)",
                  "support_threshold_angle": "40", "support_on_build_plate_only": "1"
                  if overhang_mm2 < 2000 else "0"})
        reasons["enable_support"] = f"sporgenze {overhang_mm2 / 100:.0f} cm²"
    else:
        s["enable_support"] = "0"
        reasons["enable_support"] = "nessuna sporgenza importante"
    # A1: il piatto si muove avanti e indietro, i modelli alti con poco appoggio oscillano
    slender = height / max(np.sqrt(max(contact, 1e-6)), 1e-6)
    if contact < 400 or slender > 4:
        s.update({"brim_type": "outer_only", "brim_width": "6" if slender > 6 else "4"})
        reasons["brim_type"] = f"appoggio {contact / 100:.1f} cm² per {height:.0f} mm di altezza"
    else:
        s["brim_type"] = "no_brim"
        reasons["brim_type"] = f"appoggio ampio ({contact / 100:.0f} cm²)"
    if max(ext) > A1_BED:
        bp.warn(f"Il modello ({np.round(ext).astype(int).tolist()} mm) non entra nel piatto "
                f"della A1 ({A1_BED:.0f} mm): riduci l'altezza.")
    return s, reasons


def add_print_settings(tmf, mesh, thin=None, work=None):
    """Scrive le impostazioni nel 3MF (Metadata/model_settings.config, letto da Bambu
    Studio come impostazioni dell'oggetto) e in stampa.json."""
    import zipfile
    settings, reasons = print_settings(mesh, thin)
    meta = "".join(f'    <metadata key="{k}" value="{v}"/>\n' for k, v in settings.items())
    cfg = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <object id="1">\n'
           '    <metadata key="name" value="busto"/>\n    <metadata key="extruder" value="1"/>\n'
           f'{meta}    <part id="1" subtype="normal_part">\n'
           '      <metadata key="name" value="busto"/>\n    </part>\n  </object>\n</config>\n')
    with zipfile.ZipFile(tmf, "a", zipfile.ZIP_DEFLATED) as z:
        if "Metadata/model_settings.config" not in z.namelist():
            z.writestr("Metadata/model_settings.config", cfg)
    labels = {"layer_height": "strato", "sparse_infill_density": "riempimento",
              "wall_loops": "pareti", "enable_support": "supporti", "brim_type": "brim"}
    print("Impostazioni di stampa (Bambu A1): " + ", ".join(
        f"{labels[k]} {settings[k]}" for k in labels if k in settings))
    for k, why in reasons.items():
        print(f"  {labels.get(k, k)}: {why}")
    if work:
        (Path(work) / "stampa.json").write_text(json.dumps(
            {"stampante": "Bambu Lab A1", "impostazioni": settings, "motivi": reasons},
            indent=2, ensure_ascii=False), encoding="utf-8")
    return settings


# ----------------------------------------------------------------- simmetria
def _reflect(points, n, d):
    return points - 2 * ((points @ n) - d)[:, None] * n


def _rotate(v, axis, angle):
    axis = axis / np.linalg.norm(axis)
    return (v * np.cos(angle) + np.cross(axis, v) * np.sin(angle)
            + axis * (axis @ v) * (1 - np.cos(angle)))


def symmetry_plane(mesh, side, vert):
    """Piano di simmetria (normale, distanza) vicino a quello che contiene la verticale e il
    davanti: prova inclinazioni e spostamenti e tiene quello per cui la forma specchiata
    combacia meglio con sé stessa."""
    from scipy.spatial import cKDTree
    pts = mesh.sample(6000)
    tree = cKDTree(mesh.vertices)
    side = side - (side @ vert) * vert
    side /= np.linalg.norm(side)
    front = np.cross(vert, side)
    span = np.ptp(mesh.vertices @ side)
    best = (np.inf, side, float(np.median(pts @ side)))
    for step in (np.radians(4), np.radians(1)):                   # grezzo, poi fine
        n0, d0 = best[1], best[2]
        for a in np.arange(-4, 5) * step:
            for b in np.arange(-2, 3) * step:
                n = _rotate(_rotate(n0, vert, a), front, b)
                for d in d0 + np.linspace(-1, 1, 9) * span * (0.08 if step > 0.03 else 0.02):
                    err = float(np.mean(tree.query(_reflect(pts, n, d))[0]))
                    if err < best[0]:
                        best = (err, n, d)
    return best[1], best[2]


def mirror_half(mesh, side, vert, keep="auto"):
    """Tiene una metà (la più pulita, o "destra"/"sinistra" come la vede chi guarda il
    davanti) e la specchia sull'altra."""
    import trimesh
    n, d = symmetry_plane(mesh, np.asarray(side, float), np.asarray(vert, float))
    halves = {}
    for name, sign in (("destra", 1), ("sinistra", -1)):
        h = mesh.slice_plane(n * d, n * sign, cap=False)
        # rugosità: angolo medio fra facce vicine (le zone ricostruite male sono a bozzi)
        halves[name] = (h, float(np.mean(h.face_adjacency_angles)) if len(h.faces) else np.inf)
    if keep == "auto":
        keep = min(halves, key=lambda k: halves[k][1])
    half = halves[keep][0]
    other = "sinistra" if keep == "destra" else "destra"
    print(f"Simmetria: tengo la metà {keep} (rugosità {halves[keep][1]:.3f} contro "
          f"{halves[other][1]:.3f}) e la specchio.")
    sign = 1 if keep == "destra" else -1
    span = float(np.ptp(mesh.vertices, 0).max())
    try:
        # metà chiusa un po' oltre il piano, più la sua immagine speculare: l'unione delle
        # due (che si sovrappongono appena) è un solido chiuso senza cuciture aperte
        import manifold3d as m3d
        kept = bp.cut_plane(mesh, n * sign, n * (d - sign * 0.002 * span))
        mir = trimesh.Trimesh(_reflect(kept.vertices, n, d), kept.faces[:, ::-1], process=False)

        def to_m(t):
            return m3d.Manifold(m3d.Mesh(vert_properties=np.asarray(t.vertices, np.float32),
                                         tri_verts=np.asarray(t.faces, np.uint32)))
        res = (to_m(kept) + to_m(mir)).to_mesh()
        out = trimesh.Trimesh(res.vert_properties[:, :3], res.tri_verts, process=False)
        if out.is_empty:
            raise ValueError("unione vuota")
    except Exception as e:
        print("Unione con manifold3d non riuscita, cucio le due metà:", e)
        v = half.vertices.copy()
        mirrored = trimesh.Trimesh(_reflect(v, n, d), half.faces[:, ::-1], process=False)
        out = trimesh.util.concatenate([half, mirrored])
        out.merge_vertices(digits_vertex=6)
        out.remove_unreferenced_vertices()
        if not out.is_watertight:
            trimesh.repair.fill_holes(out)
    return out


# ----------------------------------------------------------------- texture ed export
def texture_mesh(mesh, transform, work, res_level):
    """Proietta le foto sulla mesh finale (TextureMesh di OpenMVS). Ritorna la mesh con
    texture nel sistema finale (mm) o None."""
    import trimesh
    mvs = Path(work) / "mvs"
    if not (mvs / "scene.mvs").is_file():
        return None
    bp.PROGRESS.start("texture")
    raw = mesh.copy()
    raw.apply_transform(np.linalg.inv(transform))    # indietro nel sistema delle camere
    raw.export(mvs / "texture_in.ply")
    out = mvs / "texture.obj"
    for f in mvs.glob("texture*.png"):
        f.unlink()
    try:
        exe = bp.find_exe("TextureMesh")
    except SystemExit:
        return None
    args = [exe, "scene.mvs", "-m", "texture_in.ply", "-o", "texture.obj",
            "--export-type", "obj", "--resolution-level", res_level, "--close-holes", 0,
            *bp.mvs_threads()]
    help_text = bp.program_help(exe, "--help")
    if bp.has_option(help_text, "decimate"):
        args += ["--decimate", 1]
    if bp.has_option(help_text, "global-seam-leveling"):
        # in OpenMVS 2.4 il livellamento delle cuciture annerisce gran parte della texture
        args += ["--global-seam-leveling", 0, "--local-seam-leveling", 0]
    if bp.run(args, cwd=mvs, follow_log="TextureMesh", check=False) or not out.is_file():
        bp.warn("Texture non riuscita: esporto il modello senza colori.")
        return None
    tex = trimesh.load(out, force="mesh", process=False)
    tex.apply_transform(transform)
    bp.PROGRESS.update(0.8)
    return tex


def export_view(mesh, work):
    """busto.glb (browser, Windows, Android AR) e, se c'è usd-core, busto.usdz (iPhone AR).
    Le unità glTF sono metri: la scala resta reale."""
    import trimesh
    m = mesh.copy()
    m.apply_scale(0.001)
    m.apply_transform(trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0]))  # Z su -> Y su
    glb = Path(work) / "busto.glb"
    m.export(glb)
    print("Esportato", glb.name)
    try:
        export_usdz(m, Path(work) / "busto.usdz")
        print("Esportato busto.usdz")
    except ImportError:
        pass
    except Exception as e:
        print("Export USDZ non riuscito:", e)
    return glb


def export_usdz(m, path):
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils, Vt
    tmp = path.with_suffix(".usdc")
    stage = Usd.Stage.CreateNew(str(tmp))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, "/Busto")
    stage.SetDefaultPrim(root.GetPrim())
    geo = UsdGeom.Mesh.Define(stage, "/Busto/Mesh")
    geo.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(m.vertices.astype(np.float32)))
    geo.CreateFaceVertexCountsAttr(Vt.IntArray([3] * len(m.faces)))
    geo.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(m.faces.ravel().astype(np.int32)))
    mat = UsdShade.Material.Define(stage, "/Busto/Mat")
    sh = UsdShade.Shader.Define(stage, "/Busto/Mat/PBR")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.8)
    image = getattr(getattr(m.visual, "material", None), "image", None)
    uv = getattr(m.visual, "uv", None)
    files = [tmp]
    if image is not None and uv is not None:
        png = path.with_name("busto_texture.png")
        image.save(png)
        files.append(png)
        st = UsdGeom.PrimvarsAPI(geo).CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray,
                                                    UsdGeom.Tokens.vertex)
        st.Set(Vt.Vec2fArray.FromNumpy(uv.astype(np.float32)))
        reader = UsdShade.Shader.Define(stage, "/Busto/Mat/st")
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        texs = UsdShade.Shader.Define(stage, "/Busto/Mat/tex")
        texs.CreateIdAttr("UsdUVTexture")
        texs.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(png.name)
        texs.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            reader.ConnectableAPI(), "result")
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            texs.ConnectableAPI(), "rgb")
    else:
        sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.8, 0.8, 0.8))
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(geo.GetPrim()).Bind(mat)
    stage.GetRootLayer().Save()
    del stage
    ok = UsdUtils.CreateNewARKitUsdzPackage(Sdf.AssetPath(str(tmp)), str(path))
    for f in files:
        f.unlink(missing_ok=True)
    if not ok:
        raise RuntimeError("pacchetto USDZ non creato")


if __name__ == "__main__":
    sys.exit("Modulo della pipeline: usa bust_pipeline.py")
