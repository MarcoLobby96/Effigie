#!/usr/bin/env python3
"""
build_dist.py — Crea il pacchetto di installazione di Effigie.

  dist/Effigie/          cartella portatile: Python incluso, librerie, COLMAP, OpenMVS,
                         ffmpeg, esempi (si avvia con python/pythonw.exe src/app.py)
  dist/Effigie-Setup.exe installer (Inno Setup) per utente, senza diritti di amministratore

La modalità IA (torch, ~3 GB) non è inclusa: Effigie lo segnala se servirebbe.

  python scripts/build_dist.py                 cartella + installer
  python scripts/build_dist.py --solo-cartella
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP = DIST / "Effigie"
VERSION = "1.0"

# librerie della pipeline fotografica (senza la modalità IA), versioni provate
REQUIREMENTS = """numpy==2.5.4
scipy==1.18.1
pymeshlab==2025.7.post1
trimesh==5.1.1
shapely==2.2.0
mapbox-earcut==2.1.0
lxml==6.1.3
networkx==3.7
manifold3d==3.5.4
pillow==12.3.0
pillow-heif==1.8.1
rtree==1.4.1
opencv-python-headless==4.14.0.94
usd-core==26.8
"""
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pdb", "test", "tests", "idlelib",
                              "turtledemo", "ensurepip_*", "*_test.exe")


def sh(*cmd):
    print(">>", " ".join(map(str, cmd)), flush=True)
    subprocess.check_call([str(c) for c in cmd])


def base_python():
    """Cartella del Python "vero" (non del .venv): contiene anche tkinter."""
    return Path(sys.base_prefix)


def copy_python():
    src = base_python()
    print("Python di partenza:", src)
    dst = APP / "python"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "site-packages", "__pycache__", "Doc", "Tools", "test", "tests", "idlelib",
        "turtledemo", "*.pdb", "Scripts"))
    (dst / "Lib" / "site-packages").mkdir(exist_ok=True)
    py = dst / "python.exe"
    sh(py, "-m", "ensurepip", "--default-pip")
    req = DIST / "requisiti.txt"
    req.write_text(REQUIREMENTS)
    sh(py, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check",
       "-r", req)
    req.unlink()
    for junk in (dst / "Lib" / "site-packages").rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)


def copy_program():
    shutil.copytree(ROOT / "src", APP / "src", ignore=SKIP)
    shutil.copytree(ROOT / "assets", APP / "assets", ignore=SKIP)
    shutil.copy2(ROOT / "README.md", APP / "LEGGIMI.md")
    # licenze dei programmi e delle librerie inclusi (vedi README, «Ringraziamenti»)
    shutil.copytree(ROOT / "licenze", APP / "licenze")
    tools = ROOT / "tools"
    if not (tools / "colmap").is_dir():
        sys.exit("Mancano gli strumenti: esegui prima scripts/setup_tools.py")
    shutil.copytree(tools / "colmap", APP / "tools" / "colmap", ignore=SKIP)
    shutil.copytree(tools / "openmvs", APP / "tools" / "openmvs", ignore=SKIP)
    shutil.copytree(tools / "modelli", APP / "tools" / "modelli", ignore=SKIP)
    # di ffmpeg servono solo gli eseguibili
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        found = next(tools.glob(f"ffmpeg/**/{exe}"), None)
        if found:
            (APP / "tools" / "ffmpeg").mkdir(parents=True, exist_ok=True)
            shutil.copy2(found, APP / "tools" / "ffmpeg" / exe)


EXAMPLE_FILES = ("busto.stl", "busto.3mf", "busto.glb", "busto.json", "anteprima.png",
                 "stampa.json")


def add_example(name, project, photos, settings):
    """Esempio = progetto già elaborato (si apre subito) + le foto ridotte per rifarlo."""
    from PIL import Image, ImageOps
    out = APP / "Esempi" / name
    (out / "foto").mkdir(parents=True)
    for f in EXAMPLE_FILES:
        if (Path(project) / f).is_file():
            shutil.copy2(Path(project) / f, out / f)
    files = sorted(p for p in Path(photos).iterdir()
                   if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"))
    for k, f in enumerate(files, 1):
        with Image.open(f) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1600, 1600), Image.LANCZOS)
            im.save(out / "foto" / f"{name.lower()}_{k:03d}.jpg", quality=88)
    info = {"name": name, "src": "foto", "mode": "images", "created": "esempio", **settings}
    (out / "progetto.json").write_text(json.dumps(info, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"Esempio {name}: {len(files)} foto")


def examples(args):
    """Esempi da un file JSON: [{"nome", "progetto", "foto", "impostazioni"}]."""
    if not args.esempi:
        # esempi già pronti in esempi/: solo quelli con FONTE.txt (licenza che permette di
        # ridistribuirli); gli altri restano solo sul PC di chi li ha
        for ex in sorted(p for p in (ROOT / "esempi").glob("*") if p.is_dir()):
            if (ex / "FONTE.txt").is_file():
                shutil.copytree(ex, APP / "Esempi" / ex.name, ignore=SKIP)
                print("Esempio incluso:", ex.name)
            else:
                print("Esempio escluso (manca FONTE.txt):", ex.name)
        return
    for e in json.loads(Path(args.esempi).read_text(encoding="utf-8")):
        add_example(e["nome"], e["progetto"], e["foto"], e.get("impostazioni", {}))


def installer():
    iscc = next((p for p in (Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
                             Path.home() / r"AppData\Local\Programs\Inno Setup 6\ISCC.exe",
                             Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")) if p.is_file()),
                None)
    if not iscc:
        sys.exit("Inno Setup non trovato (winget install JRSoftware.InnoSetup).")
    wizard_images()
    sh(iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={APP}", f"/DOutDir={DIST}",
       ROOT / "scripts" / "effigie.iss")


def wizard_images():
    """Immagini della procedura guidata (bitmap di Inno Setup), in più risoluzioni per gli
    schermi ad alta densità: laterale con il marchio e piccola in alto a destra."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    def font(size, bold=False):
        for n in (("seguisb.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
            try:
                return ImageFont.truetype(n, size)
            except OSError:
                pass
        return ImageFont.load_default()
    with Image.open(ROOT / "assets" / "simbolo.png") as sym:
        sym = sym.convert("RGBA")
        for k in (1.0, 1.25, 1.5, 2.0):
            w, h = int(164 * k), int(314 * k)
            img = Image.new("RGB", (w, h), (22, 22, 24))
            glow = Image.new("L", (w, h), 0)
            ImageDraw.Draw(glow).ellipse((-w * 0.6, h * 0.05, w * 1.4, h * 0.75), fill=120)
            img.paste((40, 110, 220), (0, 0), glow.filter(ImageFilter.GaussianBlur(40 * k)))
            d = ImageDraw.Draw(img, "RGBA")
            for x in range(-h, w + h, int(26 * k)):              # trama a triangoli leggera
                d.line([(x, 0), (x + h // 2, h)], fill=(255, 255, 255, 9))
                d.line([(x, 0), (x - h // 2, h)], fill=(255, 255, 255, 9))
            s = sym.resize((int(118 * k), int(118 * k)), Image.LANCZOS)
            img.paste(s, ((w - s.width) // 2, int(62 * k)), s)
            d.text((w / 2, 205 * k), "Effigie", font=font(int(30 * k), True),
                   fill=(240, 242, 246), anchor="mm")
            d.text((w / 2, 232 * k), "DALLE FOTO", font=font(int(10 * k)),
                   fill=(150, 165, 185), anchor="mm")
            d.text((w / 2, 246 * k), "ALLA STAMPA 3D", font=font(int(10 * k)),
                   fill=(150, 165, 185), anchor="mm")
            name = DIST / f"wizard_{int(k * 100)}.bmp"
            img.save(name)
            n = int(55 * k)
            small = Image.new("RGB", (n, n), (255, 255, 255))
            t = sym.resize((n - 4, n - 4), Image.LANCZOS)
            small.paste(t, (2, 2), t)
            name = DIST / f"wizard_small_{int(k * 100)}.bmp"
            small.save(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-cartella", action="store_true")
    ap.add_argument("--esempi", help="file JSON con gli esempi da includere")
    args = ap.parse_args()
    shutil.rmtree(APP, ignore_errors=True)
    APP.mkdir(parents=True)
    copy_python()
    copy_program()
    examples(args)
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file()) / 1e9
    print(f"Cartella pronta: {APP} ({size:.2f} GB)")
    if not args.solo_cartella:
        installer()


if __name__ == "__main__":
    main()
