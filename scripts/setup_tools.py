#!/usr/bin/env python3
"""
Scarica in ./tools gli eseguibili esterni: COLMAP (versione senza CUDA, ok per AMD),
OpenMVS e ffmpeg. Versioni fissate (quelle provate con la pipeline) e verificate con
SHA-256. Su Windows è tutto automatico; su Linux/macOS dice cosa installare.
Si può rilanciare: salta ciò che è già presente alla versione giusta.
"""
import hashlib
import platform
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # cartella del progetto (src/ è il codice)
TOOLS = ROOT / "tools"
EXE = ".exe" if platform.system() == "Windows" else ""
STAMP = ".versione"
GITHUB = "https://github.com/{repo}/releases/download/{version}/{asset}"

# Versioni provate con la pipeline completa.
WINDOWS_TOOLS = [
    dict(name="colmap", label="COLMAP (no-CUDA)", version="4.2.1", repo="colmap/colmap",
         asset="colmap-x64-windows-nocuda.zip", probe="colmap",
         sha256="c493d88cb4a43f21afba42cb55bf786e5434a0420e73db0ba1454c5d8b3686ae"),
    dict(name="openmvs", label="OpenMVS", version="v2.4.0", repo="cdcseacave/openMVS",
         asset="OpenMVS_Windows_x64.zip", probe="DensifyPointCloud",
         sha256="0c31660c15c9ebc4c106873cf67564d9570d404aef7a6403451da1b6178b2167"),
    dict(name="ffmpeg", label="ffmpeg", version="9.0.2", repo="GyanD/codexffmpeg",
         asset="ffmpeg-9.0.2-essentials_build.zip", probe="ffmpeg",
         sha256="60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"),
]


def find_in(folder, name):
    folder = Path(folder)
    if not folder.is_dir():
        return None
    hits = [p for p in folder.rglob(f"{name}{EXE}") if p.is_file()]
    return min(hits, key=lambda p: len(str(p))) if hits else None


def installed_version(folder):
    try:
        return (Path(folder) / STAMP).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def download(url, dest, sha256=None):
    try:
        digest = _download(url, dest)
        if sha256 and digest != sha256:
            raise OSError(f"file scaricato corrotto o diverso dall'atteso (SHA-256 {digest})")
    except BaseException:
        dest.unlink(missing_ok=True)      # niente archivi a metà
        raise


def _download(url, dest):
    print(f"  scarico {url}")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "busto3d-setup"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total:3d}%  ({done >> 20} MB)", end="", flush=True)
    print()
    return h.hexdigest()


def install(tool):
    folder = TOOLS / tool["name"]
    if installed_version(folder) == tool["version"] and find_in(folder, tool["probe"]):
        return print(f"[ok] {tool['label']} {tool['version']} già presente")
    print(f"[..] {tool['label']} {tool['version']}")
    TOOLS.mkdir(exist_ok=True)
    archive = TOOLS / tool["asset"]
    download(GITHUB.format(**tool), archive, tool["sha256"])
    # estrae in una cartella temporanea: un'estrazione interrotta non sembra mai "installata"
    tmp = TOOLS / f"{tool['name']}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    finally:
        archive.unlink(missing_ok=True)
    if not find_in(tmp, tool["probe"]):
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError(f"{tool['probe']}{EXE} non trovato nell'archivio {tool['asset']}")
    (tmp / STAMP).write_text(tool["version"], encoding="utf-8")
    replace_dir(tmp, folder)
    print(f"[ok] {tool['label']} {tool['version']}")


def replace_dir(src, dst, attempts=20):
    """Sposta src in dst. Su Windows l'antivirus blocca per qualche istante gli
    eseguibili appena estratti: riprova, e come ultima risorsa copia."""
    for _ in range(attempts):
        shutil.rmtree(dst, ignore_errors=True)
        try:
            src.rename(dst)
            return
        except OSError:
            time.sleep(0.5)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    shutil.rmtree(src, ignore_errors=True)


# modelli piccoli, uguali per tutti i sistemi
MODELS = [
    dict(name="yunet.onnx", label="riconoscimento dei volti (YuNet, OpenCV Zoo)",
         url="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx",
         sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
]


def models():
    folder = TOOLS / "modelli"
    folder.mkdir(parents=True, exist_ok=True)
    for m in MODELS:
        dest = folder / m["name"]
        if dest.is_file():
            continue
        print(f"[..] {m['label']}")
        try:
            download(m["url"], dest, m["sha256"])
            print()
        except (OSError, urllib.error.URLError) as e:    # facoltativo: si va avanti senza
            print(f"\n[!] {m['label']} non scaricato ({e}): il davanti si sceglie a mano.")


def windows():
    for tool in WINDOWS_TOOLS:
        install(tool)


def unix():
    missing = [n for n in ("colmap", "DensifyPointCloud", "ffmpeg")
               if not (find_in(TOOLS, n) or shutil.which(n))]
    if not missing:
        return print("[ok] tutti i tool esterni trovati")
    print("Mancano:", ", ".join(missing))
    if platform.system() == "Linux":
        print("  sudo apt install colmap ffmpeg      (Ubuntu/Debian)")
        print("  OpenMVS: compila da https://github.com/cdcseacave/openMVS")
        print(f"           oppure copia gli eseguibili in {TOOLS}/openmvs")
    else:
        print("  brew install colmap ffmpeg   e OpenMVS da sorgente")


def main():
    try:
        (windows if platform.system() == "Windows" else unix)()
        models()
    except urllib.error.URLError as e:
        sys.exit(f"\nErrore durante il download: {e.reason}\nControlla la connessione e riprova.")
    except OSError as e:
        sys.exit(f"\nErrore durante l'installazione degli strumenti: {e}")
    print("\nSetup strumenti completato.")


if __name__ == "__main__":
    main()
