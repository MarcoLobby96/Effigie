#!/usr/bin/env python3
"""
Avvio con un clic: crea l'ambiente .venv, installa le librerie Python,
scarica i tool esterni (solo la prima volta) e apre l'interfaccia.
Usa solo la libreria standard, così parte con qualunque Python >= 3.10.
"""
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # cartella del progetto (src/ è il codice)
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
STAMP = VENV / ".installato"
CHECK = "import numpy, scipy, pymeshlab, trimesh, manifold3d, PIL, rtree"


def sh(*cmd):
    print(">>", " ".join(map(str, cmd)), flush=True)
    subprocess.check_call([str(c) for c in cmd], cwd=ROOT)


def unpinned(req):
    """requirements senza versioni fissate: per Python più nuovi delle versioni provate."""
    out = VENV / "requirements-libere.txt"
    lines = [ln.split("==")[0].strip() for ln in req.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith(("#", "-"))]
    out.write_text("\n".join(lines) + "\n")
    return out


def install(req):
    sh(PY, "-m", "pip", "install", "--upgrade", "pip")
    try:
        sh(PY, "-m", "pip", "install", "-r", req)
    except subprocess.CalledProcessError:
        print("Versioni provate non installabili con questo Python: uso le più recenti.")
        sh(PY, "-m", "pip", "install", "-r", unpinned(req))


def main():
    if sys.version_info < (3, 10):
        sys.exit("Serve Python 3.10 o superiore (python.org).")
    if not PY.exists():
        print("Creo l'ambiente virtuale .venv ...")
        venv.create(VENV, with_pip=True)
    req = ROOT / "requirements.txt"
    ok = subprocess.run([str(PY), "-c", CHECK], capture_output=True).returncode == 0
    if not ok or not STAMP.exists() or STAMP.stat().st_mtime < req.stat().st_mtime:
        install(req)
        STAMP.touch()
    else:
        print("Librerie Python già installate.")
    sh(PY, ROOT / "scripts" / "setup_tools.py")
    print("Avvio l'interfaccia ...")
    # pythonw + processo staccato: l'interfaccia resta aperta anche chiudendo il terminale
    gui = PY.with_name("pythonw.exe") if sys.platform == "win32" else PY
    if not gui.exists():
        gui = PY
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             if sys.platform == "win32" else 0)
    log = open(ROOT / "app_errori.log", "w", encoding="utf-8")
    subprocess.Popen([str(gui), str(ROOT / "src" / "app.py")], cwd=ROOT, stdout=log, stderr=log,
                     creationflags=flags)


if __name__ == "__main__":
    main()
