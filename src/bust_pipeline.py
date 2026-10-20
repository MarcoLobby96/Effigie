#!/usr/bin/env python3
"""
bust_pipeline.py — Da foto/video a busto stampabile, aperto in Bambu Studio.

Pipeline (tutta su CPU: funziona con GPU AMD, nessun CUDA richiesto):
  1. immagini: dal video i frame più nitidi (ffmpeg), dalle foto la rotazione EXIF;
     tutto ridimensionato in <out>/images
  2. COLMAP: posizioni delle camere + nuvola sparsa (SIFT su CPU), allineamento
     alla gravità
  4. OpenMVS: nuvola densa (CPU, multi-thread)
  5. ritaglio dello sfondo attorno alla testa, superficie chiusa (Poisson),
     orientamento automatico dalle camere, base piatta, scala all'altezza in mm
  6. export STL + 3MF e apertura in Bambu Studio

Requisiti (setup_tools.py li scarica in ./tools, oppure nel PATH):
  COLMAP 3.x/4.x, OpenMVS 2.x (InterfaceCOLMAP, DensifyPointCloud), ffmpeg (solo video)
  pip install -r requirements.txt

Esempi:
  python bust_pipeline.py --video giro_testa.mp4 --out lavoro --height 120
  python bust_pipeline.py --images foto/ --out lavoro --height 150 --cut 0.12
  python bust_pipeline.py --images foto/ --out lavoro --from-step 5 --up z
"""

import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

# output UTF-8 anche quando è letto da una pipe (interfaccia) su Windows
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent      # cartella del progetto (src/ è il codice)
TOOLS_DIR = ROOT / "tools"
WINDOWS = os.name == "nt"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}
UP_VECTORS = {"x": [1, 0, 0], "-x": [-1, 0, 0], "y": [0, 1, 0],
              "-y": [0, -1, 0], "z": [0, 0, 1], "-z": [0, 0, -1]}
# qualità -> (lato massimo immagini px, resolution-level OpenMVS, profondità Poisson, facce)
QUALITY = {"bozza": (1280, 1, 9, 150_000),
           "media": (1600, 1, 10, 300_000),
           "alta": (2400, 0, 11, 800_000)}


SHOOTING_TIP = ("Serve un video (o 50-100 foto) girando attorno alla testa: la persona resta "
                "ferma e un'altra persona la riprende facendo 2-3 giri lenti. I selfie non "
                "funzionano: si muove la testa e resta ferma la stanza.")


def clean_path(p):
    """Toglie spazi e virgolette (\"Copia come percorso\" di Windows le aggiunge)."""
    return p.strip().strip('"').strip("'").strip() if p else p


# ----------------------------------------------------------------- messaggi per l'interfaccia
def emit(tag, *parts):
    """Riga di controllo letta da app.py: @@STEP, @@PROGRESS, @@WARN, @@DONE."""
    print(f"@@{tag}", *parts, flush=True)


def warn(msg):
    emit("WARN", msg)


STAGES = {  # fase: (inizio %, fine %, descrizione)
    "images": (0, 8, "Preparazione immagini"),
    "features": (8, 14, "COLMAP: punti caratteristici"),
    "matching": (14, 32, "COLMAP: confronto tra le immagini"),
    "mapper": (32, 46, "COLMAP: posizione delle camere"),
    "undistort": (46, 48, "COLMAP: correzione delle immagini"),
    "densify": (48, 88, "OpenMVS: nuvola densa"),
    "ai": (48, 88, "IA: immagino la forma 3D"),
    "surface": (88, 95, "Superficie, base e scala"),
    "texture": (95, 100, "Colori dalle foto ed export"),
}


class Progress:
    def __init__(self):
        self.stage = None
        self.shown = None

    def start(self, stage):
        self.stage, self.shown = stage, None
        self.update(0)

    def update(self, frac):
        if self.stage is None:
            return
        lo, hi, label = STAGES[self.stage]
        pct = int(lo + (hi - lo) * min(max(frac, 0.0), 1.0))
        if pct != self.shown:
            self.shown = pct
            emit("PROGRESS", pct, self.stage, label)


PROGRESS = Progress()


# ----------------------------------------------------------------- programmi esterni
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GLOG = re.compile(r"^([IWEF])\d{8} [\d:.]+\s+\d+ [^\]]+\] ")
OPENMVS_PREFIX = re.compile(r"^\d\d:\d\d:\d\d \[[^\]]*\] ")
NOISE = re.compile(r"MEMORYINFO|ENDINFO|^\t|^\s*$|Creating SIFT CPU|Reference image|"
                   r"^\s+(Name|Dimensions|Camera|Focal Length|Features|GPS):")
EXIT_HINTS = {
    0xC0000135: "manca una libreria di sistema: installa Microsoft Visual C++ Redistributable "
                "x64 (https://aka.ms/vs/17/release/vc_redist.x64.exe)",
    0xC0000005: "il programma si è bloccato",
    0xC0000017: "memoria insufficiente: prova la qualità 'bozza'",
}


def clean_line(line):
    line = ANSI.sub("", line).rstrip()
    m = GLOG.match(line)
    if m:
        return ("ERRORE: " if m.group(1) in "EF" else "") + line[m.end():]
    return OPENMVS_PREFIX.sub("", line)


class LineHandler:
    """Stampa le righe ripulite; quelle di avanzamento solo ogni 10% per non intasare il log."""

    def __init__(self, parse=None):
        self.parse = parse
        self.lock = threading.Lock()
        self.decile = None

    def __call__(self, raw):
        line = clean_line(raw)
        if NOISE.search(line):
            return
        with self.lock:
            frac = self.parse(line) if self.parse else None
            if frac is not None:
                PROGRESS.update(frac)
                if int(frac * 10) == self.decile:
                    return
                self.decile = int(frac * 10)
            print(line, flush=True)


class LogTail:
    """Segue il file <programma>-*.log: su Windows OpenMVS scrive solo lì, non sull'output."""

    def __init__(self, folder, program):
        self.folder, self.program = Path(folder), program
        self.before = set(self.folder.glob(f"{program}-*.log"))
        self.file, self.pos, self.rest = None, 0, b""

    def lines(self, final=False):
        if self.file is None:
            new = [p for p in self.folder.glob(f"{self.program}-*.log") if p not in self.before]
            if not new:
                return []
            self.file = max(new, key=lambda p: p.stat().st_mtime)
        try:
            with open(self.file, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        self.pos += len(data)
        *lines, self.rest = (self.rest + data).split(b"\n")
        if final and self.rest:
            lines, self.rest = lines + [self.rest], b""
        return [x.decode("utf-8", "replace").rstrip("\r") for x in lines]


def no_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def run(cmd, cwd=None, parse=None, follow_log=None, check=True):
    """Esegue un programma mostrando l'output ripulito e aggiornando l'avanzamento.
    I programmi girano a priorità bassa: il PC resta usabile durante l'elaborazione."""
    cmd = [str(c) for c in cmd]
    print("\n>>", " ".join(cmd), flush=True)
    handle = LineHandler(parse)
    tail = LogTail(cwd or ".", follow_log) if follow_log and WINDOWS else None
    if WINDOWS:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS}
    else:
        flags = {"preexec_fn": lambda: os.nice(10)}
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", **flags)
    except OSError as e:
        sys.exit(f"Impossibile avviare {Path(cmd[0]).name}: {e}")
    reader = threading.Thread(target=lambda: [handle(x) for x in p.stdout], daemon=True)
    reader.start()
    while p.poll() is None:
        for x in tail.lines() if tail else ():
            handle(x)
        time.sleep(0.25)
    reader.join(10)
    for x in tail.lines(final=True) if tail else ():
        handle(x)
    code = p.returncode
    if code and check:
        hint = EXIT_HINTS.get(code & 0xFFFFFFFF)
        sys.exit(f"Errore nel comando {Path(cmd[0]).name} (codice {code})"
                 + (f": {hint}" if hint else ""))
    return code


THREADS = 0                     # core da usare (0 = tutti)


def limit_cpu(n):
    """Limita la pipeline (e i programmi che avvia, che lo ereditano) a n core logici."""
    global THREADS
    total = os.cpu_count() or 1
    if not 0 < n < total:
        return
    THREADS = n
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = str(n)
    try:
        if WINDOWS:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            k32.SetProcessAffinityMask(k32.GetCurrentProcess(), (1 << n) - 1)
        else:
            os.sched_setaffinity(0, range(n))
        print(f"Uso {n} core su {total}.")
    except (OSError, AttributeError) as e:
        print("Limite della CPU non applicato:", e)


def mvs_threads():
    return ["--max-threads", THREADS] if THREADS else []


def find_exe(name, folder=None):
    exts = [".exe", ""] if WINDOWS else [""]
    # 1) cartella tools/ del progetto (scaricata da setup_tools.py)
    if TOOLS_DIR.exists():
        for e in exts:
            hits = [h for h in TOOLS_DIR.rglob(f"{name}{e}") if h.is_file()]
            if hits:
                return str(sorted(hits, key=lambda h: len(str(h)))[0])
    if folder:
        for e in exts:
            p = Path(folder) / f"{name}{e}"
            if p.exists():
                return str(p)
    p = shutil.which(name)
    if not p:
        sys.exit(f"Non trovo '{name}'. Esegui setup_tools.py oppure indica il percorso "
                 "con l'opzione apposita.")
    return p


_HELP = {}


def program_help(exe, *args):
    """Testo dell'help di un programma (OpenMVS lo scrive in un file di log)."""
    key = (exe, args)
    if key not in _HELP:
        text = ""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            try:
                r = subprocess.run([exe, *args], cwd=tmp, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60, **no_window())
                text = r.stdout + r.stderr
            except (OSError, subprocess.TimeoutExpired):
                pass
            for f in Path(tmp).glob("*.log"):
                text += f.read_text(encoding="utf-8", errors="replace")
        _HELP[key] = text
    return _HELP[key]


def has_option(help_text, option):
    return re.search(rf"--{re.escape(option)}\b", help_text) is not None


def counter(pattern):
    """Avanzamento da righe tipo 'Processed file [12/48]'."""
    rx = re.compile(pattern)

    def parse(line):
        m = rx.search(line)
        return int(m.group(1)) / max(int(m.group(2)), 1) if m else None
    return parse


# ----------------------------------------------------------------- step 1: immagini
def count_images(folder):
    folder = Path(folder)
    return sum(1 for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXTS) if folder.is_dir() else 0


def sharpness(path):
    """Nitidezza = varianza del laplaciano (i frame mossi hanno valori bassi)."""
    from PIL import Image
    with Image.open(path) as im:
        im.draft("L", (640, 640))           # JPEG: decodifica veloce a risoluzione ridotta
        g = np.asarray(im.convert("L"), np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def ffmpeg_progress():
    total = []

    def parse(line):
        hms = r"(\d+):(\d+):(\d+(?:\.\d+)?)"
        if m := re.search("Duration: " + hms, line):
            total[:] = [int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])]
        elif total and total[0] > 0 and (m := re.search("time=" + hms, line)):
            return 0.8 * (int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])) / total[0]
        return None
    return parse


def video_duration(video):
    """Durata del video in secondi (None se non si riesce a leggerla)."""
    try:
        probe = find_exe("ffprobe")
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(video)], capture_output=True,
                           text=True, timeout=60, **no_window())
        return float(r.stdout.strip())
    except (SystemExit, OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:                                     # senza ffprobe: la durata la stampa ffmpeg
        r = subprocess.run([find_exe("ffmpeg"), "-hide_banner", "-i", str(video)],
                           capture_output=True, text=True, timeout=60, errors="replace",
                           **no_window())
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    except (SystemExit, OSError, subprocess.TimeoutExpired):
        return None


def extract_frames(video, out, fps, max_size, max_frames, work):
    """Estrae 3 frame per ogni frame da tenere e tiene il più nitido di ogni gruppo.
    fps = 0: frame/s scelti dalla durata, per avere circa max_frames immagini
    (un video di 10 secondi ne dà quante uno di un minuto)."""
    over = 3
    if fps <= 0:
        duration = video_duration(video)
        fps = min(max(max_frames / duration, 0.3), 10.0) if duration else 2.0
        print(f"Video di {duration or 0:.0f} s: estraggo {fps:.2g} frame/s.")
    tmp = work / "frames_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    scale = (f"scale='if(gte(iw,ih),min(iw,{max_size}),-2)'"
             f":'if(gte(iw,ih),-2,min(ih,{max_size}))'")
    run([find_exe("ffmpeg"), "-hide_banner", "-y", "-i", video,
         "-vf", f"fps={fps * over:g},{scale}", "-qscale:v", "2", tmp / "f_%05d.jpg"],
        parse=ffmpeg_progress())
    frames = sorted(tmp.glob("f_*.jpg"))
    if not frames:
        sys.exit("ffmpeg non ha estratto nessun frame: il video è leggibile?")
    groups = [frames[i:i + over] for i in range(0, len(frames), over)]
    best = []
    for k, g in enumerate(groups, 1):
        best.append(max(g, key=sharpness))
        PROGRESS.update(0.8 + 0.2 * k / len(groups))
    if len(best) > max_frames:
        best = [best[i] for i in np.linspace(0, len(best) - 1, max_frames).round().astype(int)]
    for k, f in enumerate(best, 1):
        os.replace(f, out / f"img_{k:04d}.jpg")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"Tenuti {len(best)} frame nitidi su {len(frames)} estratti.")
    return len(best)


def convert_photos(src, out, max_size):
    """Applica la rotazione EXIF (COLMAP la ignora) e ridimensiona. Ritorna
    (numero foto, calibrazione): True = una sola fotocamera, "folders" = un gruppo per
    fotocamera/orientamento (sottocartelle), False = ogni foto calibrata a sé."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if not files:
        sys.exit(f"Nessuna foto trovata in {src} (formati: jpg, png, heic, tif, webp).")

    def convert(item):
        k, f = item
        try:
            with Image.open(f) as im:
                exif = im.getexif()
                im = ImageOps.exif_transpose(im).convert("RGB")
                cam = (exif.get(0x0110), exif.get_ifd(0x8769).get(0x920A), im.size)
                im.thumbnail((max_size, max_size), Image.LANCZOS)
                # tiene l'EXIF (la focale dell'obiettivo aiuta COLMAP a partire bene), con
                # l'orientamento azzerato perché la foto è già stata raddrizzata
                exif[0x0112] = 1
                try:
                    im.save(out / f"img_{k:04d}.jpg", quality=95, exif=exif.tobytes())
                except Exception:
                    im.save(out / f"img_{k:04d}.jpg", quality=95)
            return cam
        except Exception as e:
            warn(f"Foto ignorata ({f.name}): {e}")
            return None

    cams = []
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        for i, cam in enumerate(pool.map(convert, enumerate(files, 1)), 1):
            cams.append(cam)
            PROGRESS.update(i / len(files))
    ok = [c for c in cams if c]
    print(f"Preparate {len(ok)} foto su {len(files)}.")
    groups = sorted(set(ok), key=str)
    if len(groups) <= 1:
        return len(ok), True
    sizes = [sum(c == g for c in ok) for g in groups]
    if len(groups) > 6 or min(sizes) < 3:
        return len(ok), False
    # stesso telefono ma foto verticali e orizzontali (o pochi zoom diversi): un gruppo
    # per tipo, ognuno con la sua calibrazione condivisa (più stabile di una per foto)
    for k, cam in enumerate(cams, 1):
        if cam:
            sub = out / f"gruppo{groups.index(cam) + 1}"
            sub.mkdir(exist_ok=True)
            os.replace(out / f"img_{k:04d}.jpg", sub / f"img_{k:04d}.jpg")
    print(f"Foto divise in {len(groups)} gruppi (orientamento/fotocamera): {sizes}")
    return len(ok), "folders"


def prepare_images(a, work, max_size):
    print("@@STEP 1", flush=True)
    PROGRESS.start("images")
    out = work / "images"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if a.video:
        n, single_camera = extract_frames(Path(a.video), out, a.fps, max_size, a.max_frames, work), True
    else:
        n, single_camera = convert_photos(Path(a.images), out, max_size)
        if single_camera is False:
            warn("Foto da molte fotocamere/zoom diversi: calibro ogni foto separatamente.")
    if n == 0:
        sys.exit("Nessuna immagine utilizzabile.")
    import extras
    extras.check_photos(out, work)
    n = count_images(out)
    if n < 3:
        warn(f"Solo {n} immagini: uso l'IA, che immagina le parti non viste (risultato "
             f"approssimativo). {SHOOTING_TIP}")
    elif n < 10:
        warn(f"Solo {n} immagini: provo la fotogrammetria, se non basta passo all'IA.")
    elif n < 20:
        warn(f"Solo {n} immagini: la ricostruzione potrebbe fallire (meglio 50-100).")
    return single_camera


# ----------------------------------------------------------------- step 2-3: COLMAP
class Colmap:
    """Usa i nomi delle opzioni supportati dalla versione installata (3.x o 4.x)."""

    def __init__(self, exe):
        self.exe = exe

    def has(self, command, option):
        return has_option(program_help(self.exe, command, "-h"), option)

    def pick(self, command, *options):
        return next((o for o in options if self.has(command, o)), None)

    def cpu_only(self, command, *options):
        opt = self.pick(command, *options)
        return [f"--{opt}", "0"] if opt else []

    def run(self, command, args, parse=None, check=True):
        extra = []
        if self.has(command, "log_target"):
            extra += ["--log_target", "stderr"]
        if self.has(command, "log_color"):
            extra += ["--log_color", "0"]
        return run([self.exe, command, *args, *extra], parse=parse, check=check)


def registered_count(images_bin):
    try:
        with open(images_bin, "rb") as f:
            return struct.unpack("<Q", f.read(8))[0]
    except (OSError, struct.error):
        return 0


def colmap_sfm(exe, work, single_camera, max_size):
    print("@@STEP 2", flush=True)
    img_dir = work / "images"
    n_images = count_images(img_dir)
    db = work / "database.db"
    sparse, aligned, dense = work / "sparse", work / "sparse_aligned", work / "dense"
    # da zero: un database vecchio farebbe saltare le immagini con lo stesso nome
    for p in (db, work / "database.db-shm", work / "database.db-wal"):
        p.unlink(missing_ok=True)
    for d in (sparse, aligned, dense, work / "sparse_global"):
        shutil.rmtree(d, ignore_errors=True)
    sparse.mkdir(parents=True)
    aligned.mkdir()
    c = Colmap(exe)

    PROGRESS.start("features")
    c.run("feature_extractor",
          ["--database_path", db, "--image_path", img_dir,
           *(["--ImageReader.single_camera_per_folder", 1] if single_camera == "folders" else
             ["--ImageReader.single_camera", int(bool(single_camera))]),
           "--ImageReader.camera_model", "OPENCV" if single_camera else "SIMPLE_RADIAL",
           *c.cpu_only("feature_extractor", "FeatureExtraction.use_gpu", "SiftExtraction.use_gpu")],
          parse=counter(r"Processed file \[(\d+)/(\d+)\]"))

    PROGRESS.start("matching")
    matcher = "exhaustive_matcher" if n_images <= 150 else "sequential_matcher"
    block = re.compile(r"(?:Processing|Matching) block \[(\d+)/(\d+), (\d+)/(\d+)\]")

    def matching_progress(line):
        if m := block.search(line):
            i, n, j, k = map(int, m.groups())
            return ((i - 1) * k + j - 1) / (n * k)
        return counter(r"(?:Processing|Matching) image \[(\d+)/(\d+)\]")(line)
    c.run(matcher, ["--database_path", db,
                    *c.cpu_only(matcher, "FeatureMatching.use_gpu", "SiftMatching.use_gpu")],
          parse=matching_progress)

    PROGRESS.start("mapper")
    reg = re.compile(r"Registering image #\d+ \((?:num_reg_frames=)?(\d+)\)")
    c.run("mapper", ["--database_path", db, "--image_path", img_dir, "--output_path", sparse],
          parse=lambda line: int(m[1]) / n_images if (m := reg.search(line)) else None)
    models = [(registered_count(p / "images.bin"), p) for p in sparse.iterdir()
              if (p / "images.bin").is_file()]
    if (not models or max(m[0] for m in models) < 0.9 * n_images) and \
            has_option(program_help(exe, "global_mapper", "-h"), "output_path"):
        # Il metodo incrementale a volte si blocca dopo poche foto (dipende da scelte
        # casuali nei confronti); quello globale risolve tutto il giro insieme.
        print("Poche foto agganciate: riprovo con il metodo globale di COLMAP.")
        glob_dir = work / "sparse_global"
        shutil.rmtree(glob_dir, ignore_errors=True)
        glob_dir.mkdir()
        c.run("global_mapper", ["--database_path", db, "--image_path", img_dir,
                                "--output_path", glob_dir], check=False)
        models += [(registered_count(p / "images.bin"), p) for p in glob_dir.iterdir()
                   if (p / "images.bin").is_file()]
    if not models or max(m[0] for m in models) < 5:
        sys.exit("COLMAP non è riuscito a ricostruire la scena: le immagini sono troppo poche "
                 f"o troppo diverse tra loro. {SHOOTING_TIP}")
    n_reg, best = max(models, key=lambda m: m[0])
    print(f"COLMAP ha posizionato {n_reg} immagini su {n_images}.")
    if n_reg < 0.5 * n_images:
        warn(f"Usate solo {n_reg} immagini su {n_images}: gira più lentamente, con più luce "
             "e senza riflessi; il risultato potrebbe essere incompleto.")

    args = ["--image_path", img_dir, "--input_path", best, "--output_path", aligned]
    if c.has("model_orientation_aligner", "method"):
        args += ["--method", "IMAGE-ORIENTATION"]          # riprese tenute dritte
    if c.run("model_orientation_aligner", args, check=False) or \
            not (aligned / "images.bin").is_file():
        warn("Allineamento alla gravità non riuscito: uso il modello non allineato.")
        shutil.rmtree(aligned, ignore_errors=True)
        shutil.copytree(best, aligned)

    PROGRESS.start("undistort")
    c.run("image_undistorter",
          ["--image_path", img_dir, "--input_path", aligned, "--output_path", dense,
           "--output_type", "COLMAP", "--max_image_size", max_size],
          parse=counter(r"Undistorting image \[(\d+)/(\d+)\]"))
    return dense


# ----------------------------------------------------------------- step 4: OpenMVS
def openmvs_dense(mvs_dir, dense, work, resolution_level):
    print("@@STEP 4", flush=True)
    PROGRESS.start("densify")
    mvs = work / "mvs"
    shutil.rmtree(mvs, ignore_errors=True)
    mvs.mkdir()
    run([find_exe("InterfaceCOLMAP", mvs_dir), "-i", dense, "-o", mvs / "scene.mvs",
         "--image-folder", dense / "images"], cwd=mvs, follow_log="InterfaceCOLMAP")
    exe = find_exe("DensifyPointCloud", mvs_dir)
    args = [exe, "scene.mvs", "--resolution-level", resolution_level, *mvs_threads()]
    help_text = program_help(exe, "--help")
    if has_option(help_text, "tower-mode"):
        args += ["--tower-mode", "0"]        # niente punti artificiali "a torre"
    if has_option(help_text, "filter-point-cloud"):
        # filtro di visibilità: sulla volpe superficie il 26% meno rugosa, stesso tempo
        args += ["--filter-point-cloud", "1"]
    n = max(count_images(dense / "images"), 1)
    done = set()

    def parse(line):
        if m := re.search(r"Depth-map for image\s+(\d+) estimated", line):
            done.add(m[1])
            return 0.9 * len(done) / n
        if "fused and filtered" in line:
            return 0.97
        if m := re.search(r"(\d+(?:\.\d+)?)%", line):    # versioni con barra di avanzamento
            return float(m[1]) / 100
        return None
    run(args, cwd=mvs, follow_log="DensifyPointCloud", parse=parse)
    cloud = mvs / "scene_dense.ply"
    if not cloud.is_file():
        sys.exit("OpenMVS non ha prodotto la nuvola densa.")
    return cloud


def latest_input(work):
    """Nuvola densa di OpenMVS (o, per cartelle di versioni precedenti, l'ultima mesh)."""
    mvs = Path(work) / "mvs"
    found = [f for f in (Path(work) / "ia.ply", mvs / "scene_dense.ply") if f.is_file()]
    if found:                                   # forma dell'IA o nuvola densa: la più recente
        return max(found, key=os.path.getmtime)
    meshes = sorted(mvs.glob("*mesh*.ply"), key=os.path.getmtime)
    return meshes[-1] if meshes else None


# ----------------------------------------------------------------- camere
def qvec_to_rotmat(q):
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                     [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                     [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def read_cameras(images_bin):
    """(nome, centro, direzione di vista, direzione 'su', direzione 'destra') delle camere di un images.bin
    di COLMAP, in ordine di nome (= ordine di ripresa)."""
    cams = []
    with open(images_bin, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            f.read(4)                                          # image_id
            q = struct.unpack("<4d", f.read(32))
            t = np.array(struct.unpack("<3d", f.read(24)))
            f.read(4)                                          # camera_id
            name = bytearray()
            while (ch := f.read(1)) not in (b"\0", b""):
                name += ch
            (npts,) = struct.unpack("<Q", f.read(8))
            f.seek(24 * npts, 1)
            R = qvec_to_rotmat(q)
            cams.append((name.decode("utf-8", "replace"), -R.T @ t, R[2], -R[1], R[0]))
    return sorted(cams, key=lambda c: Path(c[0]).name)


def horizon_vertical(U, Rt, D):
    """Verticale dal bordo orizzontale delle foto: chi scatta non inclina il telefono di
    lato, quindi quel bordo resta orizzontale anche guardando in basso e la verticale è
    perpendicolare a tutti. Riconosce da sola le foto ruotate di 90° (senza EXIF), per
    cui il bordo orizzontale è l'altro. None se le camere non danno una risposta chiara."""
    best = None
    for start in (Rt, U):                 # ipotesi: nessuna foto ruotata / foto ruotate
        A = start
        for _ in range(8):
            w, V = np.linalg.eigh(A.T @ A / len(A))
            v = V[:, 0]
            upright = np.abs(Rt @ v) <= np.abs(U @ v)          # quale bordo è orizzontale
            A = np.where(upright[:, None], Rt, U)
        if w[1] < 0.1:                    # bordi tutti paralleli: verticale non definita
            continue
        fit = float(np.mean(np.minimum(np.abs(Rt @ v), np.abs(U @ v))))
        # verso: dal "su" delle foto dritte; se sono tutte ruotate, si guarda dall'alto
        if upright.sum() >= max(2, 0.2 * len(U)):
            v = v * (np.sign(np.sum((U @ v)[upright])) or 1.0)
        else:
            v = v * (-np.sign(np.mean(D @ v)) or 1.0)
        looks_up = float(np.mean(D @ v > 0.2))             # camere che guardano in alto
        score = fit + 0.3 * looks_up + 0.02 * float(np.mean(~upright))
        if best is None or score < best[0]:
            best = (score, v, fit)
    if best is None or best[2] > 0.08:
        return None
    return best[1]


def camera_frame(cams):
    """Punto guardato da tutte le camere (minimi quadrati), distanza tipica, 'su' medio.
    None se le camere non girano attorno a un soggetto."""
    if len(cams) < 3:
        return None
    C = np.array([c[1] for c in cams])
    D = np.array([c[2] for c in cams])
    U = np.array([c[3] for c in cams])
    Rt = np.array([c[4] for c in cams]) if len(cams[0]) > 4 else None
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for c, d in zip(C, D):
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ c
    if np.linalg.cond(A) > 1e3:
        return None
    center = np.linalg.solve(A, b)
    if np.median(((center - C) * D).sum(1)) <= 0:        # il punto dev'essere davanti
        return None
    up = U.mean(0)
    consistency = float(np.linalg.norm(up))
    up = up / max(consistency, 1e-12)
    # Camere su un anello che guardano di lato (giro attorno all'oggetto): l'asse
    # dell'anello è la verticale, anche se le foto sono state scattate ruotate.
    rel = C - center
    _, S, Vt = np.linalg.svd(rel - rel.mean(0))
    axis = Vt[2]
    # riprese dall'alto verso un oggetto sul tavolo, su un arco: l'asse dell'arco è la
    # verticale se le camere guardano in giù lungo l'asse e il bordo alto delle foto ha
    # una parte verso l'alto (di fronte a una facciata resterebbe perpendicolare all'asse)
    a = axis * (np.sign(np.mean(U @ axis)) or 1.0)
    from_above = (S[2] < 0.5 * S[1] and np.mean(D @ a) < -0.3 and np.mean(U @ a) > 0.3
                  and (Rt is None or np.mean(np.abs(Rt @ a)) < 0.3))
    if from_above:
        up, consistency = a, 1.0
    elif S[2] < 0.35 * S[1] and np.mean(np.abs(D @ axis)) < 0.5:
        if consistency > 0.5 and abs(up @ axis) > 0.5:
            axis = axis * np.sign(up @ axis)               # verso dal "su" delle camere
        else:
            axis = axis * (np.sign(np.mean(rel @ axis)) or 1.0)  # di solito si sta sopra
        up, consistency = axis, 1.0
    elif Rt is not None:
        v = horizon_vertical(U, Rt, D)
        if v is not None:
            up, consistency = v, 1.0
    return {"center": center, "dist": float(np.median(np.linalg.norm(C - center, axis=1))),
            "up": up, "up_consistency": consistency,
            "first": C[0], "cams": C}


def read_intrinsics(model_dir):
    """{nome immagine: (fx, fy, cx, cy)} da cameras.bin e images.bin di COLMAP."""
    nparams = {0: 3, 1: 4, 2: 4, 3: 5, 4: 8, 5: 8, 6: 12, 7: 5, 8: 4, 9: 5, 10: 12}
    cams = {}
    with open(model_dir / "cameras.bin", "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            cid, model = struct.unpack("<ii", f.read(8))
            f.read(16)                                         # larghezza, altezza
            p = struct.unpack(f"<{nparams[model]}d", f.read(8 * nparams[model]))
            cams[cid] = (p[0], p[0], p[1], p[2]) if model in (0, 2, 3, 8, 9) else p[:4]
    out = {}
    with open(model_dir / "images.bin", "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        for _ in range(n):
            f.read(4 + 32 + 24)
            (cid,) = struct.unpack("<i", f.read(4))
            name = bytearray()
            while (ch := f.read(1)) not in (b"\0", b""):
                name += ch
            (npts,) = struct.unpack("<Q", f.read(8))
            f.seek(24 * npts, 1)
            out[name.decode("utf-8", "replace")] = cams.get(cid)
    return out


def triangulate(rays):
    """Punto più vicino a tutte le semirette (origine, direzione)."""
    A, b = np.zeros((3, 3)), np.zeros(3)
    for c, d in rays:
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ c
    return np.linalg.solve(A, b) if np.linalg.cond(A) < 1e6 else None


def face_front(work, frame, cams_file):
    """Direzione del davanti dal volto (busti, statue, persone, animali). Il rilevatore
    YuNet di OpenCV trova occhi e naso nelle foto (girate con la verticale della scena in
    alto); i punti vengono portati in 3D incrociando le linee di vista delle camere e il
    davanti è perpendicolare alla linea degli occhi, verso il naso. None se non c'è un
    volto chiaro (o mancano OpenCV o il modello)."""
    model = TOOLS_DIR / "modelli" / "yunet.onnx"
    try:
        import cv2
        detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7)
        intr = read_intrinsics(cams_file.parent)
    except Exception:                         # OpenCV, modello o calibrazione mancanti
        return None
    up = frame["up"]
    img_dir = cams_file.parent.parent / "images"         # foto raddrizzate da COLMAP
    rays = {"re": [], "le": [], "nose": []}
    for name, C, D, U, Rt in read_cameras(cams_file):
        path = img_dir / name
        img = cv2.imread(str(path)) if path.is_file() and intr.get(name) else None
        if img is None:
            continue
        h0, w0 = img.shape[:2]
        k = int(round(np.degrees(np.arctan2(Rt @ up, U @ up)) / 90)) % 4
        img = np.ascontiguousarray(np.rot90(img, k))
        scale = 640 / max(img.shape[:2])
        img = cv2.resize(img, None, fx=scale, fy=scale)
        detector.setInputSize((img.shape[1], img.shape[0]))
        _, faces = detector.detect(img)
        if faces is None or not len(faces):
            continue
        f = faces[np.argmax(faces[:, -1])]
        fx, fy, cx, cy = intr[name]
        for key, (x, y) in (("re", f[4:6]), ("le", f[6:8]), ("nose", f[8:10])):
            x, y = x / scale, y / scale
            for i in range(k, 0, -1):                     # coordinate nella foto non girata
                wp = w0 if (i - 1) % 2 == 0 else h0
                x, y = wp - 1 - y, x
            d = (x - cx) / fx * Rt - (y - cy) / fy * U + D   # riga 1 di R = -U
            rays[key].append((C, d / np.linalg.norm(d)))
    if len(rays["nose"]) < 2:
        return None
    pts = {k: triangulate(v) for k, v in rays.items()}
    if any(p is None for p in pts.values()):
        return None
    eyes = pts["le"] - pts["re"]
    front = np.cross(up, eyes)
    front -= (front @ up) * up
    if np.linalg.norm(front) < 1e-9:
        return None
    front /= np.linalg.norm(front)
    if (pts["nose"] - (pts["le"] + pts["re"]) / 2) @ front < 0:
        front = -front
    print(f"Volto riconosciuto in {len(rays['nose'])} foto: lo metto davanti.")
    return front


def load_frame(work):
    for model in (work / "dense" / "sparse", work / "sparse_aligned"):
        f = model / "images.bin"
        if f.is_file():
            try:
                return camera_frame(read_cameras(f)), f
            except (OSError, struct.error, np.linalg.LinAlgError):
                pass
    return None, None


# ----------------------------------------------------------------- step 5: superficie
def load_points(path, frame):
    """Punti e normali dalla nuvola densa (o dai vertici di una mesh)."""
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(path))
    if ms.current_mesh().face_number() > 0:
        ms.compute_normal_per_vertex()
    m = ms.current_mesh()
    v, n = m.vertex_matrix().copy(), m.vertex_normal_matrix().copy()
    lengths = np.linalg.norm(n, axis=1)
    if len(v) and not (np.isfinite(lengths).all() and np.median(lengths) > 0.5):
        print("Nuvola senza normali: le stimo.")
        ms.compute_normal_for_point_clouds(k=12)
        n = ms.current_mesh().vertex_normal_matrix().copy()
        ref = frame["center"] if frame else v.mean(0)
        n[((v - ref) * n).sum(1) < 0] *= -1                  # verso l'esterno
    return v, n


def poisson(points, normals, depth, faces, out):
    import pymeshlab
    ms = pymeshlab.MeshSet()
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=points, v_normals_matrix=normals))
    # 4 campioni per cella (invece di 1.5) e peso dei punti 1 (invece di 4): superficie
    # meno granulosa sulle nuvole rumorose (pelo, superfici opache) con gli stessi
    # dettagli; provato su volpe e Buddha
    ms.generate_surface_reconstruction_screened_poisson(depth=depth, preclean=True,
                                                        samplespernode=float(os.environ.get("EFFIGIE_SPN", 4)),
                                                        pointweight=1.0)
    # Le zone piccole mai viste (sotto il mento, fra le orecchie e il supporto) restano
    # chiuse dalla superficie liscia di Poisson: toglierle e tappare i buchi creava lame
    # sottili e tappi piatti. Le grandi "vele" inventate lontano da tutti i punti (il lato di
    # una siepe tolta, il cielo sopra un oggetto) invece si tolgono.
    remove_membranes(ms, points)
    finish_surface(ms, faces)
    ms.save_current_mesh(str(out))


def remove_membranes(ms, points):
    import pymeshlab
    from scipy.spatial import cKDTree
    m = ms.current_mesh()
    v, f = m.vertex_matrix(), m.face_matrix()
    diag = float(np.linalg.norm(np.ptp(points, 0)))
    sample = points[np.random.default_rng(0).choice(len(points), min(len(points), 400_000),
                                                    replace=False)]
    dist, _ = cKDTree(sample).query(v, workers=-1)
    far = dist > 0.04 * diag
    drop = far[f].all(1)
    if drop.sum() < 0.002 * len(f):
        return
    keep = f[~drop]
    ms.add_mesh(pymeshlab.Mesh(vertex_matrix=v, face_matrix=keep))
    ms.meshing_remove_unreferenced_vertices()
    ms.meshing_remove_connected_component_by_diameter(
        mincomponentdiag=pymeshlab.PercentageValue(10))
    print(f"Membrane di Poisson rimosse: {int(drop.sum())} facce lontane dai punti.")


def finish_surface(ms, faces):
    # dove la superficie è aperta (cubo di calcolo, membrane tolte) chiude i buchi
    # (seconda passata anche a costo di piccole auto-intersezioni, lontane dal soggetto)
    ms.meshing_close_holes(maxholesize=100_000)
    ms.meshing_close_holes(maxholesize=100_000, selfintersection=False)
    # i tappi restano selezionati e con una selezione attiva la decimazione di MeshLab
    # lavora solo su quella: senza azzerarla il modello restava pesantissimo
    ms.set_selection_none()
    if ms.current_mesh().face_number() > faces:
        # preservetopology: senza, la decimazione crea spigoli condivisi da 3+ facce e il
        # modello non si può più chiudere né tagliare in modo pulito
        ms.meshing_decimation_quadric_edge_collapse(targetfacenum=faces, preservenormal=True,
                                                    preservetopology=True)
    make_manifold(ms)


def make_manifold(ms):
    """Toglie spigoli/vertici condivisi da troppe facce (impediscono un modello chiuso).
    I buchini che restano li chiude tidy_mesh: chiuderli qui con MeshLab può far
    crashare il programma su certe configurazioni."""
    ms.meshing_remove_duplicate_faces()
    ms.meshing_repair_non_manifold_edges()
    # vertici separati spostati di pochissimo: se restassero nella stessa posizione
    # verrebbero riuniti più avanti ricreando il problema
    ms.meshing_repair_non_manifold_vertices(vertdispratio=1e-4)
    ms.meshing_remove_unreferenced_vertices()


def tidy_mesh(mesh):
    """Chiude i buchini e mette tutte le facce nello stesso verso (trimesh, sicuro)."""
    import trimesh
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    trimesh.repair.fix_winding(mesh)
    return mesh


def find_planes(points, cams, up, scale, seed=0):
    """Piani grandi vicino al soggetto (pavimento, tavolo, muro) con RANSAC.
    Ritorna [(normale verso le camere, punto, orizzontale?)]."""
    rng = np.random.default_rng(seed)
    sample = points[rng.choice(len(points), min(len(points), 60_000), replace=False)]
    thresh = 0.01 * scale
    planes = []
    for _ in range(3):
        best = (0, None, None)
        for _ in range(400):
            a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
            n = np.cross(b - a, c - a)
            if np.linalg.norm(n) < 1e-12:
                continue
            n /= np.linalg.norm(n)
            count = int((np.abs((sample - a) @ n) < thresh).sum())
            if count > best[0]:
                best = (count, n, a)
        count, n, a = best
        if n is None or count < 0.05 * min(len(points), 60_000):
            break
        inl = sample[np.abs((sample - a) @ n) < thresh]
        center = inl.mean(0)
        n = np.linalg.svd(inl - center, full_matrices=False)[2][2]     # minimi quadrati
        side = (cams - center) @ n
        if np.median(side) < 0:
            n, side = -n, -side
        d = (sample - center) @ n
        behind, front = (d < -2 * thresh).mean(), (d > 2 * thresh).mean()
        if (side > 0).mean() < 0.8 or behind > 0.05 or front < 0.25:
            # camere da entrambe le parti, o tanti punti dietro: non è pavimento/tavolo/muro
            # ma una zona poco curva del soggetto; quasi niente davanti: il piano è il
            # soggetto stesso (una facciata) e non va tolto
            sample = sample[np.abs((sample - center) @ n) >= thresh]
            continue
        tilt = abs(n @ up)
        if 0.4 < tilt <= 0.85:
            # né pavimento né parete (piano inclinato: un libro, una scatola, il soggetto):
            # non lo si usa per tagliare
            sample = sample[np.abs((sample - center) @ n) >= thresh]
            continue
        planes.append((n, center, tilt > 0.85))
        sample = sample[(sample - center) @ n > 2 * thresh]
        if len(sample) < 1000:
            break
    return planes


def find_ground(points, up, scale, seed=0):
    """Piano d'appoggio orizzontale in fondo al soggetto, anche quando è solo una striscia
    (tavolo visto quasi di taglio): RANSAC sui punti più bassi, piani quasi orizzontali."""
    rng = np.random.default_rng(seed)
    h = points @ up
    low = points[h < np.percentile(h, 25)]
    if len(low) < 500:
        return None
    sample = low[rng.choice(len(low), min(len(low), 30_000), replace=False)]
    thresh = 0.01 * scale
    best = (0, None, None)
    for _ in range(300):
        a, b, c = sample[rng.choice(len(sample), 3, replace=False)]
        n = np.cross(b - a, c - a)
        if np.linalg.norm(n) < 1e-12:
            continue
        n /= np.linalg.norm(n)
        if abs(n @ up) < 0.9:
            continue
        count = int((np.abs((sample - a) @ n) < thresh).sum())
        if count > best[0]:
            best = (count, n, a)
    count, n, a = best
    if n is None:
        return None
    n = n * np.sign(n @ up)
    inliers = np.abs((points - a) @ n) < thresh
    level = (a @ n - np.percentile(h, 0)) / max(np.ptp(h), 1e-9)
    # abbastanza punti, proprio in fondo, e quasi niente sotto
    below = ((points - a) @ n < -3 * thresh).mean()
    if inliers.mean() < 0.02 or level > 0.15 or below > 0.03:
        return None
    return n, points[inliers].mean(0)


def isolate_subject(points, normals, frame):
    """Toglie pavimento/tavolo/muro e tiene il blocco di punti connesso più grande."""
    from scipy import ndimage
    planes = find_planes(points, frame["cams"], frame["up"], frame["dist"]) if frame else []
    if frame and frame["up_consistency"] > 0.5 and not any(h for _, _, h in planes):
        ground = find_ground(points, frame["up"], frame["dist"])
        if ground:
            planes.append((ground[0], ground[1], True))
    for n, p0, horizontal in planes:
        d = (points - p0) @ n
        noise = np.std(d[np.abs(d) < 0.01 * frame["dist"]])      # spessore reale del piano
        front = d > max(0.02 * frame["dist"], 3 * noise)
        if front.sum() < 1000:
            continue
        points, normals = points[front], normals[front]
        print(f"{'Piano d appoggio' if horizontal else 'Parete'} trovato e rimosso: "
              f"restano {len(points)} punti.".replace("d appoggio", "d'appoggio"))
    # foto scattate tutto attorno: un piano verticale (siepe, muro dietro, fiancata di
    # un'auto) è solo sfondo da togliere. Il "retro piatto da muro" vale quando si fotografa
    # da un lato solo (trofei, targhe, facciate): altrimenti taglierebbe il soggetto a metà
    if frame and any(not h for _, _, h in planes):
        rel = frame["cams"] - frame["center"]
        rel = rel - np.outer(rel @ frame["up"], frame["up"])
        rel /= np.linalg.norm(rel, axis=1, keepdims=True) + 1e-12
        if np.linalg.norm(rel.mean(0)) < 0.5:
            planes = [pl for pl in planes if pl[2]]
            print("Foto tutto attorno: il piano verticale è sfondo, non un retro da muro.")
    # blocchi connessi su una griglia di voxel: via i pezzi staccati dal soggetto
    voxel = (frame["dist"] if frame else np.ptp(points, axis=0).max()) / 120
    idx = np.floor((points - points.min(0)) / voxel).astype(int)
    grid = np.zeros(idx.max(0) + 1, bool)
    grid[tuple(idx.T)] = True
    labels, count = ndimage.label(grid, np.ones((3, 3, 3)))
    if count > 1:
        lab = labels[tuple(idx.T)]
        main = np.bincount(lab)[1:].argmax() + 1
        if frame:
            # il soggetto è dove guardano le foto: il blocco con più punti vicino al centro
            # di vista (una recinzione o una base attorno possono essere più grandi)
            near = np.linalg.norm(points - frame["center"], axis=1) < 0.25 * frame["dist"]
            if near.sum() >= 1000:
                central = np.bincount(lab[near], minlength=count + 1)[1:].argmax() + 1
                if central != main:
                    print("Il blocco più grande non è al centro delle foto: tengo quello "
                          "centrale.")
                main = central
        keep = lab == main
        if keep.sum() >= 1000:
            points, normals = points[keep], normals[keep]
            print(f"Tenuto il blocco principale: {len(points)} punti.")
    return points, normals, planes


def relief_back(points, frame, up):
    """Ripresa da un lato solo (facciata, rilievo, solo il davanti di qualcosa): il retro
    non è mai stato visto e Poisson lo chiuderebbe a caso, con fogli che passano davanti al
    soggetto. Aggiunge invece un retro piatto: punti finti su un piano dietro il soggetto e
    sui fianchi, con le normali verso l'esterno, così la superficie diventa un blocco
    pieno tipo targa. Ritorna (punti, normali, normale del piano, punto del piano) o None."""
    from scipy import ndimage
    from scipy.spatial import cKDTree
    rel = frame["cams"] - frame["center"]
    dirs = rel / np.linalg.norm(rel, axis=1, keepdims=True)
    horiz = dirs - np.outer(dirs @ up, up)
    horiz /= np.linalg.norm(horiz, axis=1, keepdims=True) + 1e-12
    if np.linalg.norm(horiz.mean(0)) < 0.6:
        return None                                   # camere tutto attorno al soggetto
    a = dirs.mean(0)
    a /= np.linalg.norm(a)
    h = a - (a @ up) * up
    if np.linalg.norm(h) > 0.7:                       # ripreso di fronte: retro verticale
        a = h / np.linalg.norm(h)
    e1 = np.cross(up, a)
    if np.linalg.norm(e1) < 1e-6:
        e1 = np.cross([1.0, 0, 0], a)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(a, e1)
    c = points.mean(0)
    rel = points - c
    t, uv = rel @ a, np.c_[rel @ e1, rel @ e2]
    sub = points[:: max(1, len(points) // 20000)]
    spacing = float(np.median(cKDTree(sub).query(sub, 2)[0][:, 1]))
    size = float(np.ptp(uv, axis=0).max())
    cell = max(2.0 * spacing, size / 300)
    lo = uv.min(0) - 4 * cell
    ij = np.floor((uv - lo) / cell).astype(int)
    mask = np.zeros(ij.max(0) + 5, bool)
    mask[tuple(ij.T)] = True
    mask = ndimage.binary_closing(mask, iterations=2)
    mask = ndimage.binary_fill_holes(mask)
    # la sagoma comprende tutto il soggetto (già un pezzo unico in 3D): via solo le
    # briciole, insieme ai loro punti (senza retro diventerebbero pinne sottili)
    lab, n = ndimage.label(mask)
    if n > 1:
        area = np.bincount(lab.ravel())
        small = np.nonzero(area < 0.02 * area[1:].sum())[0]
        mask &= ~np.isin(lab, small[small > 0])
    if mask.sum() < 50:
        return None
    keep = mask[tuple(ij.T)]
    t_back = float(np.percentile(t, 0.5)) - 0.04 * size     # spessore della targa dietro
    ii, jj = np.nonzero(mask)
    uvc = lo + (np.c_[ii, jj] + 0.5) * cell
    back = c + uvc[:, :1] * e1 + uvc[:, 1:] * e2 + t_back * a
    pts, nrm = [back], [np.tile(-a, (len(back), 1))]
    # fianchi: dal bordo della sagoma fino al piano, normali verso l'esterno
    edge = mask & ~ndimage.binary_erosion(mask)
    smooth = ndimage.gaussian_filter(mask.astype(float), 1.5)
    gi, gj = np.gradient(smooth)
    ii, jj = np.nonzero(edge)
    g = -np.c_[gi[ii, jj], gj[ii, jj]]
    g /= np.linalg.norm(g, axis=1, keepdims=True) + 1e-12
    uvb = lo + (np.c_[ii, jj] + 0.5) * cell
    pick = np.random.default_rng(0).choice(len(uv), min(len(uv), 300_000), replace=False)
    tp = t[pick]
    near = cKDTree(uv[pick]).query_ball_point(uvb, r=1.5 * cell)
    # profondità del soggetto sul bordo, stimata per difetto: un fianco troppo alto
    # lascerebbe una pinna sottile, uno un po' basso Poisson lo raccorda alla superficie;
    # dove sul bordo non ci sono punti veri, nessun fianco
    t_edge = np.array([np.percentile(tp[i], 20) if len(i) >= 3 else np.nan for i in near])
    t_edge = t_back + 0.9 * (t_edge - t_back)
    steps = np.where(np.isnan(t_edge), 0, np.maximum((t_edge - t_back) / cell, 0)).astype(int)
    if steps.sum():
        idx = np.repeat(np.arange(len(uvb)), steps)
        frac = np.concatenate([np.arange(1, s + 1) / (s + 1) for s in steps if s])
        tt = t_back + frac * (t_edge[idx] - t_back)
        pts.append(c + uvb[idx, :1] * e1 + uvb[idx, 1:] * e2 + tt[:, None] * a)
        nrm.append(g[idx, :1] * e1 + g[idx, 1:] * e2)
    print(f"Ripreso da un lato solo: retro piatto (rilievo), {sum(map(len, pts))} punti aggiunti.")
    return np.vstack(pts), np.vstack(nrm), a, c + t_back * a, keep


def remove_outliers(points, normals, k=8, sigma=2.0):
    """Toglie i punti isolati (rumore della nuvola densa, che crea schegge)."""
    from scipy.spatial import cKDTree
    d = cKDTree(points).query(points, k + 1, workers=-1)[0][:, 1:].mean(1)
    keep = d < d.mean() + sigma * d.std()
    print(f"Punti isolati rimossi: {(~keep).sum()}")
    return points[keep], normals[keep]


def render_preview(mesh, path, size=420):
    """Anteprima PNG del modello visto da davanti, destra, dietro e sinistra
    (campionamento fitto dei triangoli + z-buffer, ombreggiatura morbida tipo creta)."""
    from PIL import Image
    ss = 2
    W, H = size * ss, int(size * 1.3) * ss
    lo, hi = mesh.bounds
    span = max(hi - lo) * 1.1
    scale = W / span
    tri = mesh.vertices[mesh.faces]
    vn = mesh.vertex_normals[mesh.faces]
    # griglia di punti su ogni triangolo, fitta quanto basta a coprire i suoi pixel
    # (i triangoli grandi, come i tappi piatti, ne ricevono di più)
    edge = np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2).max(1) * scale
    per_tri = np.clip(np.ceil(edge * 1.3), 1, 48).astype(int)
    while ((per_tri + 1) * (per_tri + 2) // 2).sum() > 7_000_000 and per_tri.max() > 1:
        per_tri = np.maximum(1, (per_tri * 0.8).astype(int))
    pts, nrm, facing = [], [], []
    for n in np.unique(per_tri):
        sel = np.nonzero(per_tri == n)[0]
        bc = np.array([(i, j, n - i - j) for i in range(n + 1) for j in range(n + 1 - i)],
                      float) / n
        pts.append(np.einsum("kb,fbd->fkd", bc, tri[sel]).reshape(-1, 3))
        nrm.append(np.einsum("kb,fbd->fkd", bc, vn[sel]).reshape(-1, 3))
        facing.append(np.repeat(mesh.face_normals[sel], len(bc), axis=0))
    pts, nrm, facing = np.vstack(pts), np.vstack(nrm), np.vstack(facing)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    key = np.array([0.45, -1.0, 0.65])
    fill_light = np.array([-0.7, -0.5, 0.2])
    key, fill_light = key / np.linalg.norm(key), fill_light / np.linalg.norm(fill_light)
    views = []
    for az in (0, 90, 180, 270):
        a = np.radians(az)
        R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
        p = pts @ R.T
        nn = nrm @ R.T
        mid = R @ ((lo + hi) / 2)
        u = ((p[:, 0] - mid[0]) * scale + W / 2).astype(np.int64)
        v = (H / 2 - (p[:, 2] - mid[2]) * scale).astype(np.int64)
        ok = (u >= 0) & (u < W) & (v >= 0) & (v < H) & ((facing @ R.T)[:, 1] < 0.1)
        pix = v[ok] * W + u[ok]
        order = np.lexsort((p[ok, 1], pix))                    # il più vicino per primo
        first = order[np.r_[True, pix[order][1:] != pix[order][:-1]]]
        nf = nn[ok][first]
        shade = 0.18 + 0.62 * np.clip(nf @ key, 0, 1) + 0.25 * np.clip(nf @ fill_light, 0, 1)
        img = np.full(W * H, np.nan)
        img[pix[first]] = 25 + 200 * np.clip(shade, 0, 1)
        img = img.reshape(H, W)
        for _ in range(2):                                  # chiude eventuali buchini
            pad = np.pad(img, 1, constant_values=np.nan)
            nb = np.stack([pad[1 + dy:H + 1 + dy, 1 + dx:W + 1 + dx]
                           for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
            holes = np.isnan(img) & (np.sum(~np.isnan(nb), 0) >= 5)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                img[holes] = np.nanmean(nb, 0)[holes]
        img = np.nan_to_num(img, nan=255).reshape(H // ss, ss, W // ss, ss).mean((1, 3))
        views.append(img)
    Image.fromarray(np.hstack(views).astype(np.uint8)).save(path)


def smooth_surface(mesh, amount):
    """Levigatura che conserva le forme: prima rende uniformi le normali delle facce
    vicine (salvo dove cambiano bruscamente: naso, orecchie, spigoli), poi sposta i
    vertici per seguirle. Toglie la grana del pelo e delle superfici opache senza
    arrotondare i dettagli come farebbe una media semplice; un passaggio Taubin finale
    leviga quel che resta senza restringere il modello."""
    import pymeshlab
    import trimesh
    steps = max(1, int(round(amount * 10)))
    try:
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices, face_matrix=mesh.faces))
        # normali uniformate poco e solo fra facce quasi allineate (40°): via la grana,
        # restano i dettagli piccoli e tondi (riccioli del Buddha, occhi, narici)
        ms.apply_coord_two_steps_smoothing(stepsmoothnum=steps, normalthr=40,
                                           stepnormalnum=int(round(4 + 14 * amount)),
                                           stepfitnum=20)
        m = ms.current_mesh()
        out = trimesh.Trimesh(m.vertex_matrix(), m.face_matrix(), process=False)
        if len(out.faces) != len(mesh.faces) or not np.isfinite(out.vertices).all():
            raise ValueError("levigatura non valida")
        mesh = out
    except Exception as e:                       # mai bloccare il modello per la levigatura
        print("Levigatura a due passi non riuscita, uso quella semplice:", e)
    trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=max(2, steps))
    print(f"Levigatura: {steps} passate.")
    return mesh


def main_parts(mesh, share=0.15):
    """Tutti i pezzi grandi della superficie, non solo il più grande: un soggetto scuro su
    una base chiara (cavallo di bronzo sul piedistallo) può uscire staccato dalla base,
    e tenendo solo il pezzo maggiore si perdeva proprio il soggetto."""
    import trimesh
    parts = mesh.split(only_watertight=False)
    if len(parts) <= 1:
        return mesh
    big = max(p.area for p in parts)
    keep = [p for p in parts if p.area >= share * big]
    if len(keep) > 1:
        print(f"Tenuti {len(keep)} pezzi grandi della superficie.")
    return trimesh.util.concatenate(keep)


def largest_part(mesh):
    parts = mesh.split(only_watertight=False)
    return max(parts, key=lambda m: m.area) if len(parts) else mesh


def cut_base(mesh, z_cut):
    """Toglie tutto sotto z_cut lasciando una base piatta chiusa."""
    return cut_plane(mesh, [0.0, 0.0, 1.0], [0.0, 0.0, z_cut])


def cut_plane(mesh, normal, origin):
    """Tiene la parte dal lato di `normal` del piano, chiusa con una faccia piatta."""
    import trimesh
    try:
        import manifold3d as m3d
        man = m3d.Manifold(m3d.Mesh(vert_properties=np.asarray(mesh.vertices, np.float32),
                                    tri_verts=np.asarray(mesh.faces, np.uint32)))
        if man.status() != m3d.Error.NoError:
            raise ValueError(man.status())
        normal = np.asarray(normal, float) / np.linalg.norm(normal)
        out = man.trim_by_plane(normal.tolist(), float(normal @ np.asarray(origin, float))).to_mesh()
        # process=False: superfici sottilissime che si toccano restano separate (unirne i
        # vertici le renderebbe non chiuse)
        res = trimesh.Trimesh(out.vert_properties[:, :3], out.tri_verts, process=False)
        if not res.is_empty:
            return res
    except Exception as e:                                    # es. mesh non chiusa
        print("Taglio con manifold3d non riuscito, uso trimesh:", e)
    res = mesh.slice_plane(origin, normal, cap=True)
    if not res.is_watertight:
        trimesh.repair.fill_holes(res)
    return res


def clean_plinth(mesh):
    """Il soggetto su un piedistallo tagliato esce sopra una lastra bucata e frastagliata
    (il piano del piedistallo visto solo dall'alto): se in basso c'è una lastra larga e
    sopra la sezione crolla di colpo (zampe, piedi), la lastra diventa un plinto pieno
    con il contorno pulito, unito al soggetto."""
    import trimesh
    import manifold3d as m3d
    from shapely.geometry import MultiPoint
    zmin, zmax = mesh.bounds[:, 2]
    height = zmax - zmin
    zs = np.linspace(zmin, zmin + 0.35 * height, 141)[1:]
    area = np.zeros(len(zs))
    for i, z in enumerate(zs):
        s = mesh.section([0, 0, 1], [0, 0, z])
        if s is not None:
            area[i] = s.to_2D()[0].area
    top = int(area.argmax())
    if area[top] <= 0:
        return mesh
    above = np.nonzero(area[top:] < 0.15 * area[top])[0]
    if not len(above):
        return mesh
    z_top = zs[top + above[0]]
    rest = area[top + above[0]:]
    # sopra la lastra deve esserci qualcosa di stretto ma presente (zampe, non vuoto)
    if z_top - zmin > 0.25 * height or not len(rest) or rest.max() > 0.3 * area[top]:
        return mesh
    slab = mesh.vertices[mesh.vertices[:, 2] < z_top]
    if len(slab) < 10:
        return mesh
    foot = MultiPoint(slab[:, :2]).convex_hull.simplify(0.003 * height)
    if foot.geom_type != "Polygon" or foot.area < 0.5 * area[top]:
        return mesh
    lift = 0.012 * height                       # via anche le bozze sul piano della lastra
    try:
        cut = z_top + lift
        subject = cut_plane(mesh, [0, 0, 1], [0, 0, cut])
        if subject.is_empty:
            return mesh
        ring = np.asarray(foot.exterior.coords)[:-1]
        if foot.exterior.is_ccw is False:
            ring = ring[::-1]
        base = m3d.Manifold.extrude(m3d.CrossSection([ring.tolist()]), float(cut + lift - zmin))
        base = base.translate([0.0, 0.0, float(zmin)])
        top_m = m3d.Manifold(m3d.Mesh(vert_properties=np.asarray(subject.vertices, np.float32),
                                      tri_verts=np.asarray(subject.faces, np.uint32)))
        out = (top_m + base).to_mesh()
        res = trimesh.Trimesh(out.vert_properties[:, :3], out.tri_verts, process=False)
    except Exception as e:
        print("Plinto pulito non riuscito:", e)
        return mesh
    if res.is_empty or not res.is_watertight:
        return mesh
    print(f"Lastra del piedistallo sostituita da un plinto pulito ({z_top - zmin:.1f} unità).")
    return res


def manual_cuts(work):
    """Tagli fatti a mano nell'app: piani (normale verso la parte da tenere, punto) nelle
    coordinate della ricostruzione."""
    try:
        cuts = json.loads((Path(work) / "tagli.json").read_text(encoding="utf-8"))
        return cuts if isinstance(cuts, list) else []
    except (OSError, ValueError):
        return []


def clean_and_prepare(raw, work, up, height_mm, cut_frac, crop, poisson_depth, faces,
                      smooth=0.3, turn=0, orient="", mask=False, marker_mm=0, texture=True,
                      res_level=1, mirror="", fill_ai=False):
    print("@@STEP 5", flush=True)
    PROGRESS.start("surface")
    import trimesh
    ai = Path(raw).name == "ia.ply"
    frame, cams_file = (None, None) if ai else load_frame(work)
    if frame is None and not ai:
        warn("Posizioni delle camere non disponibili: niente ritaglio né orientamento automatico.")
    if ai and up == "auto":
        up = "z"                                  # TripoSR restituisce i modelli con Z in su

    # Poisson è la parte lenta: se cambiano solo asse, taglio o altezza la riusa
    raw = Path(raw)
    key = {"v": 28, "input": str(raw), "mtime": raw.stat().st_mtime, "size": raw.stat().st_size,
           "crop": crop, "depth": poisson_depth, "faces": faces, "mask": mask,
           "cuts": manual_cuts(work), "fill_ai": fill_ai,
           "cams": cams_file.stat().st_mtime if cams_file else None,
           "up": np.round(frame["up"], 3).tolist() if frame else None}
    cache, cache_key = work / "poisson.ply", work / "poisson.json"
    planes = []
    relief_used = False
    try:
        saved = json.loads(cache_key.read_text())
        reuse = cache.is_file() and saved.get("key") == key
        if reuse:
            planes = [(np.array(n), np.array(c), h) for n, c, h in saved.get("planes", [])]
            relief_used = bool(saved.get("relief"))
    except (OSError, ValueError, AttributeError):
        reuse = False
    if ai:
        cache = raw                               # forma già chiusa: niente Poisson
    elif reuse:
        print("Riuso la superficie già calcolata (cambiati solo orientamento/taglio/altezza).")
    else:
        points, normals = load_points(raw, frame)
        print(f"Punti: {len(points)}")
        if mask and frame and (work / "dense" / "masks").is_dir():
            import extras
            points, normals = extras.apply_masks(points, normals, work)
        PROGRESS.update(0.15)
        if frame and crop > 0:
            # via lo sfondo: tiene solo ciò che sta vicino al punto guardato dalle camere
            keep = np.linalg.norm(points - frame["center"], axis=1) < crop * frame["dist"]
            if keep.sum() >= 1000:
                points, normals = points[keep], normals[keep]
                print(f"Ritaglio attorno al soggetto: tenuti {keep.sum()} punti.")
            else:
                warn("Il ritaglio toglierebbe quasi tutto: lo salto (aumenta 'Ritaglio sfondo').")
        points, normals, planes = isolate_subject(points, normals, frame)
        cuts = manual_cuts(work)
        if cuts:
            # i tagli a mano tolgono i punti prima della superficie: tutta la risoluzione
            # va alla parte che resta (un busto sopra un piedistallo esce molto più definito)
            keep = np.ones(len(points), bool)
            margin = 0.01 * float(np.ptp(points, 0).max())
            for c in cuts:
                n = np.asarray(c["n"], float)
                keep &= (points - np.asarray(c["p"], float)) @ n > -margin
            if keep.sum() >= 1000:
                points, normals = points[keep], normals[keep]
                print(f"Tagli a mano: tenuti {keep.sum()} punti per la superficie.")
        points, normals = remove_outliers(points, normals)
        if len(points) < 1000:
            sys.exit("Troppo pochi punti per ricostruire la superficie.")
        if len(points) > 3_000_000:                       # oltre non migliora, rallenta solo
            sel = np.random.default_rng(0).choice(len(points), 3_000_000, replace=False)
            points, normals = points[sel], normals[sel]
        PROGRESS.update(0.25)
        if fill_ai and ai_available():
            # l'IA riempie solo le parti che le foto non hanno visto
            import ai_fill
            import ai_reconstruct
            photos = sorted(p for p in (work / "images").rglob("*")
                            if p.suffix.lower() in IMAGE_EXTS)
            fill = ai_fill.fill_points(work, points, normals, ai_reconstruct.pick_photo(photos))
            if fill is not None:
                points, normals = np.vstack([points, fill[0]]), np.vstack([normals, fill[1]])
        extra = None
        level_shots = frame and np.mean(np.abs(
            (frame["center"] - frame["cams"]) @ frame["up"]
            / np.linalg.norm(frame["center"] - frame["cams"], axis=1))) < 0.5
        if frame and level_shots and not any(not h for _, _, h in planes):
            # niente parete, camere da una parte sola e quasi in piano (facciata, volto,
            # rilievo): retro piatto. Dall'alto su un tavolo il retro è il piano d'appoggio
            up_hint = frame["up"] if up == "auto" else np.asarray(UP_VECTORS[up], float)
            extra = relief_back(points, frame, up_hint)
        if extra:
            relief_used = True
            planes.append((extra[2], extra[3], False))
            points, normals = points[extra[4]], normals[extra[4]]
            poisson(np.vstack([points, extra[0]]), np.vstack([normals, extra[1]]),
                    poisson_depth, faces, cache)
        else:
            poisson(points, normals, poisson_depth, faces, cache)
        # campione dei punti veri: serve a mettere la base al punto reale più basso
        sample = points[np.random.default_rng(0).choice(len(points), min(len(points), 30_000),
                                                         replace=False)]
        np.save(work / "punti.npy", sample.astype(np.float32))
        cache_key.write_text(json.dumps(
            {"key": key, "relief": relief_used,
             "planes": [(n.tolist(), c.tolist(), bool(h)) for n, c, h in planes]}))
    PROGRESS.update(0.75)

    mesh = tidy_mesh(main_parts(trimesh.load(cache, force="mesh")))
    if smooth > 0:
        mesh = smooth_surface(mesh, smooth)
    ground = next(((n, c) for n, c, h in planes if h), None)
    if mesh.is_empty:
        sys.exit("La ricostruzione Poisson ha prodotto una mesh vuota.")
    transform = np.eye(4)

    def apply(M):
        nonlocal transform
        mesh.apply_transform(M)
        transform = M @ transform

    # "su" della ricostruzione su +Z (convenzione degli slicer)
    if up == "auto":
        if ground is not None:
            up_vec = ground[0]                       # il pavimento dà la verticale esatta
        elif frame and frame["up_consistency"] > 0.5:
            up_vec = frame["up"]
        else:
            warn("Asse 'su' non stimabile dalle camere: uso -y (prova a cambiarlo).")
            up_vec = UP_VECTORS["-y"]
    else:
        up_vec = UP_VECTORS[up]
    apply(trimesh.geometry.align_vectors(up_vec, [0, 0, 1]))
    wall = next((n for n, c, h in planes if not h), None)
    front = None
    if wall is not None:
        front = wall                       # esce dalla parete: il davanti di un busto a muro
    elif frame:
        # camere quasi tutte da una parte (facciata, volto scolpito, solo il davanti):
        # il davanti è da dove si è fotografato; giro completo: la prima ripresa
        rel = frame["cams"] - frame["center"]
        side = (rel / np.linalg.norm(rel, axis=1, keepdims=True)).mean(0)
        horizontal = (transform[:3, :3] @ side)[:2]
        face = face_front(work, frame, cams_file) if np.linalg.norm(horizontal) <= 0.4 else None
        if face is not None:
            front = face                      # giro completo: davanti dove c'è il volto
        else:
            front = side if np.linalg.norm(horizontal) > 0.4 else frame["first"] - frame["center"]
            if np.linalg.norm(horizontal) <= 0.4:
                # giro completo senza volto (scooter, auto, oggetti): lunghezza lungo il
                # piatto e vista principale di fianco, dal lato della prima foto
                xy = mesh.vertices[:, :2] - mesh.vertices[:, :2].mean(0)
                w, V = np.linalg.eigh(xy.T @ xy / len(xy))
                if w[1] > 1.3 * w[0]:
                    minor = np.array([V[0, 0], V[1, 0], 0.0])
                    f0 = transform[:3, :3] @ (frame["first"] - frame["center"])
                    if minor[:2] @ f0[:2] < 0:
                        minor = -minor
                    front = np.linalg.inv(transform[:3, :3]) @ minor
                    print("Nessun volto: il lato lungo va lungo il piatto.")
    if front is not None:
        # davanti del piatto (-Y), dove guarda la vista principale di Bambu Studio
        f = transform[:3, :3] @ front
        apply(trimesh.transformations.rotation_matrix(-np.pi / 2 - np.arctan2(f[1], f[0]),
                                                      [0, 0, 1]))
    vert = np.array([0.0, 0.0, 1.0])              # la verticale del soggetto
    side = np.array([1.0, 0.0, 0.0])              # destra di chi guarda il davanti

    def rotate(axis, degrees):
        nonlocal vert, side
        R = trimesh.transformations.rotation_matrix(np.radians(degrees), axis)
        apply(R)
        vert = R[:3, :3] @ vert
        side = R[:3, :3] @ side
    flat_back = any(not h for n, c, h in planes)
    if flat_back and up == "auto":
        # trofeo da muro, facciata, rilievo: sdraiato sul retro piatto si stampa senza
        # supporti e non si ribalta (in piedi poggerebbe su una striscia sottile)
        rotate([1, 0, 0], -90)
        print("Retro piatto: appoggiato sul retro (si stampa senza supporti).")
    axes = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}
    for ax in orient:                              # rotazioni di 90° scelte dall'utente
        rotate(axes[ax], 90)
    if turn:
        rotate([0, 0, 1], turn)
    upright = not flat_back and not any(ax in "xy" for ax in orient)
    # pareti: taglio netto lungo il piano (retro piatto, pronto da appendere)
    for n, c, horizontal in planes:
        if not horizontal:
            nn = transform[:3, :3] @ n
            cc = (transform @ np.append(c, 1))[:3] + nn * 0.01 * mesh.extents.max()
            mesh = main_parts(cut_plane(mesh, nn, cc))
            print("Retro tagliato lungo la parete.")
    apply(trimesh.transformations.translation_matrix(-mesh.bounds[0]))

    # Poisson chiude il fondo con una "bolla": la tagliamo con base piatta.
    # Se c'era un piano d'appoggio il soggetto ci poggia sopra: base alla sua altezza.
    zmin, zmax = mesh.bounds[:, 2]
    # Base al punto reale più basso dell'oggetto: sotto c'è solo la "bolla" con cui
    # Poisson chiude la superficie. "Taglio base" toglie ancora di più.
    z_data = zmin
    try:
        pts = np.load(work / "punti.npy") if not ai else mesh.vertices
        z = (np.c_[pts, np.ones(len(pts))] @ transform.T)[:, 2]
        z_data = max(zmin, float(np.percentile(z, 0.3)))
    except (OSError, ValueError):
        pass
    if ai:
        z_data = zmin + 0.02 * (zmax - zmin)          # IA: base piatta appena sotto il modello
    z_cut = z_data + (cut_frac * (zmax - zmin) if upright else 0.0)
    if z_cut <= zmin + 1e-9:
        z_cut = None
    if ground is not None and up == "auto" and upright:
        z_ground = (transform @ np.append(ground[1], 1))[2]
        z_cut = max(z_ground, zmin) + 0.01 * (zmax - zmin)
        print("Base appoggiata sul piano trovato.")
    if z_cut is not None:
        cut = main_parts(cut_base(mesh, z_cut))
        if cut.is_empty or cut.extents[2] <= 0:
            sys.exit("Dopo il taglio non resta nulla: riduci 'Taglio base' o cambia 'Asse su'.")
        mesh = cut
    if not upright and not ai:
        # sdraiato: il fondo del soggetto è un lato. Anche lì via la chiusura di Poisson
        # sotto il punto reale più basso, più l'eventuale taglio extra
        h = mesh.vertices @ vert
        try:
            pts = np.load(work / "punti.npy")
            real = (np.c_[pts, np.ones(len(pts))] @ transform.T)[:, :3] @ vert
            bottom = max(h.min(), float(np.percentile(real, 0.3)))
        except (OSError, ValueError):
            bottom = h.min()
        level = bottom + cut_frac * np.ptp(h)
        if level > h.min() + 1e-6:
            cut = main_parts(cut_plane(mesh, vert, vert * level))
            if not cut.is_empty:
                mesh = cut
    PROGRESS.update(0.9)

    # scala: l'altezza è quella del soggetto (anche quando è sdraiato sul retro); se
    # l'utente lo ha raddrizzato a mano, quella sul piatto
    import extras
    if mirror:
        mesh = extras.mirror_half(mesh, side, vert, mirror)
    axis = vert if not any(ax in "xy" for ax in orient) else np.array([0.0, 0.0, 1.0])
    real = extras.marker_scale(work, marker_mm) if marker_mm > 0 and not ai else None
    apply(trimesh.transformations.scale_matrix(
        real or height_mm / max(np.ptp(mesh.vertices @ axis), 1e-9)))
    c = mesh.bounds.mean(0)
    apply(trimesh.transformations.translation_matrix([-c[0], -c[1], -mesh.bounds[0][2]]))
    # tagli fatti a mano nell'app (tagli.json): piani nelle coordinate della ricostruzione,
    # applicati al modello già in scala, così le misure non cambiano; poi di nuovo sul piatto
    try:
        cuts = json.loads((work / "tagli.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cuts = []
    for k in cuts if isinstance(cuts, list) else []:
        n = np.linalg.inv(transform[:3, :3]).T @ np.asarray(k["n"], float)
        o = (transform @ np.append(np.asarray(k["p"], float), 1))[:3]
        part = main_parts(cut_plane(mesh, n / np.linalg.norm(n), o))
        if not part.is_empty:
            mesh = part
    if cuts:
        if not real:                     # l'altezza scelta è quella del modello finale
            apply(trimesh.transformations.scale_matrix(
                height_mm / max(np.ptp(mesh.vertices @ axis), 1e-9)))
        c = mesh.bounds.mean(0)
        apply(trimesh.transformations.translation_matrix([-c[0], -c[1], -mesh.bounds[0][2]]))
        print(f"Tagli a mano applicati: {len(cuts)}.")
    if upright and not ai:
        mesh = clean_plinth(mesh)
    trimesh.repair.fix_normals(mesh)
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    print(f"Watertight: {mesh.is_watertight} | dimensioni (mm): {np.round(mesh.extents, 1)}"
          f" | facce: {len(mesh.faces)}"
          + (f" | volume: {mesh.volume / 1000:.0f} cm³" if mesh.is_watertight else ""))
    if not mesh.is_watertight:
        warn("Mesh non perfettamente chiusa: Bambu Studio proverà a ripararla.")
    thin = None
    try:
        thin = extras.thickness_report(mesh)
    except Exception as e:
        print("Controllo dello spessore non riuscito:", e)

    stl, tmf = work / "busto.stl", work / "busto.3mf"
    mesh.export(stl)
    try:
        mesh.export(tmf)
    except Exception as e:
        print("Export 3MF non riuscito (uso STL):", e)
        tmf = None
    if tmf:
        try:
            extras.add_print_settings(tmf, mesh, thin, work)
        except Exception as e:
            print("Impostazioni di stampa non scritte:", e)
    (work / "busto.json").write_text(json.dumps({
        "extents_mm": mesh.extents.round(2).tolist(), "watertight": bool(mesh.is_watertight),
        "faces": int(len(mesh.faces)), "transform": transform.tolist(),
        "flat_back": bool(flat_back), "relief": bool(relief_used),
        "fill": float(mesh.volume / np.prod(mesh.extents)) if mesh.is_watertight else 0.0},
        indent=2))
    try:
        render_preview(mesh, work / "anteprima.png")
        emit("PREVIEW", work / "anteprima.png")
    except Exception as e:
        print("Anteprima non riuscita:", e)
    PROGRESS.update(1)
    view = (extras.texture_mesh(mesh, transform, work, res_level) if texture and not ai
            else None)
    if view is not None:
        # colori dei vertici, salvati nelle coordinate della ricostruzione: restano validi
        # anche dopo ritocchi, tagli e rotazioni (l'app li riporta sul modello nuovo)
        try:
            cols = view.visual.to_color().vertex_colors[:, :3].astype(np.uint8)
            raw_pts = (np.c_[view.vertices, np.ones(len(view.vertices))]
                       @ np.linalg.inv(transform).T)[:, :3]
            np.savez_compressed(work / "colori.npz", points=raw_pts.astype(np.float32),
                                colors=cols)
        except Exception as e:
            print("Colori non salvati:", e)
    try:
        extras.export_view(view if view is not None else mesh, work)
    except Exception as e:
        print("Export GLB non riuscito:", e)
    return tmf or stl


# ----------------------------------------------------------------- step 6
def open_in_bambu(path, custom=None):
    print("@@STEP 6", flush=True)
    path = str(Path(path).resolve())
    system = platform.system()
    candidates = [clean_path(custom)] if custom else []
    if system == "Windows":
        candidates += [r"C:\Program Files\Bambu Studio\bambu-studio.exe",
                       r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe"]
    elif system == "Linux":
        candidates += [shutil.which("bambu-studio"), shutil.which("BambuStudio")]
    for c in filter(None, candidates):
        if Path(c).exists():
            subprocess.Popen([c, path])
            return print("Aperto in Bambu Studio:", path)
    if system == "Darwin":
        subprocess.Popen(["open", "-a", "BambuStudio", path])
        return print("Aperto in Bambu Studio:", path)
    if system == "Linux" and shutil.which("flatpak"):
        subprocess.Popen(["flatpak", "run", "com.bambulab.BambuStudio", path])
        return print("Aperto in Bambu Studio (flatpak):", path)
    if system == "Windows":
        try:
            os.startfile(path)                       # programma associato ai .3mf
            return print("Aperto con il programma predefinito:", path)
        except OSError:
            pass
    print("Bambu Studio non trovato: apri a mano", path, "(oppure usa --bambu)")


def colmap_and_openmvs(a, work, state, max_size, res_level):
    if a.from_step <= 3:
        if not count_images(work / "images"):
            sys.exit("Nessuna immagine preparata: esegui la pipeline completa (Avvia tutto).")
        try:
            single_camera = json.loads(state.read_text()).get("single_camera", True)
        except (OSError, ValueError):
            single_camera = True
        colmap_sfm(a.colmap or find_exe("colmap"), work, single_camera, max_size)
    if not (work / "dense" / "sparse").is_dir():
        sys.exit("Manca il risultato di COLMAP: riparti dallo step 2 o da Avvia tutto.")
    openmvs_dense(a.openmvs_dir, work / "dense", work, res_level)


def looks_broken(work):
    """Ricostruzione fotografica fallita? Una lastra orizzontale molto larga e bassa vuol dire
    che è stato ricostruito il pavimento e non il soggetto (soggetto che si muoveva); un
    foglio sottilissimo ripreso tutto attorno, o un modello quasi vuoto con meno del 60%
    delle foto agganciate, è incompleto. Facciate e bassorilievi restano validi."""
    try:
        info = json.loads((work / "busto.json").read_text())
    except (OSError, ValueError):
        return False
    ex, ey, ez = info.get("extents_mm", [1, 1, 1])
    fill = info.get("fill", 1.0)
    n = count_images(work / "images")
    used = registered_count(work / "sparse_aligned" / "images.bin") / n if n else 1.0
    # camere tutto attorno al soggetto? Allora il risultato deve essere un solido: un
    # foglio sottilissimo vuol dire che il soggetto si muoveva. Ripreso da un lato solo
    # (facciate, bassorilievi) un risultato sottile è normale.
    frame, _ = load_frame(work)
    around = False
    if frame:
        rel = frame["cams"] - frame["center"]
        rel -= np.outer(rel @ frame["up"], frame["up"])           # solo la parte orizzontale
        rel /= np.linalg.norm(rel, axis=1, keepdims=True) + 1e-12
        around = bool(np.linalg.norm(rel.mean(0)) < 0.5)
    return max(ex, ey) > 6 * ez or (fill < 0.02 and around) or (used < 0.6 and fill < 0.10)


def ai_available():
    """La modalità IA richiede torch (non incluso nel pacchetto di installazione)."""
    import importlib.util
    return importlib.util.find_spec("torch") is not None


AI_MISSING = ("La modalità IA non è installata in questa versione di Effigie: servono "
              "più foto (almeno 20, meglio 50-100) girando attorno al soggetto.")


def run_ai(work):
    """Forma 3D 'immaginata' dall'IA a partire dalla foto più nitida."""
    print("@@STEP 4", flush=True)
    PROGRESS.start("ai")
    import ai_reconstruct
    photos = sorted(p for p in (work / "images").rglob("*") if p.suffix.lower() in IMAGE_EXTS)
    photo = ai_reconstruct.pick_photo(photos)
    print(f"IA: uso la foto {photo.name}")
    steps = iter((0.1, 0.3, 0.6, 0.9))

    def progress(msg):
        print(msg, flush=True)
        PROGRESS.update(next(steps, 0.95))
    ai_reconstruct.generate(photo, work / "ia.ply", progress=progress)


# ----------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Foto -> busto stampabile -> Bambu Studio")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--images", help="cartella con le foto")
    src.add_argument("--video", help="video girando attorno alla testa")
    ap.add_argument("--out", default="busto_lavoro", help="cartella di lavoro")
    ap.add_argument("--fps", type=float, default=0,
                    help="frame/s estratti dal video (0 = automatico dalla durata)")
    ap.add_argument("--max-frames", type=int, default=80, help="massimo di frame dal video")
    ap.add_argument("--height", type=float, default=120, help="altezza finale in mm")
    ap.add_argument("--cut", type=float, default=0.0,
                    help="frazione di altezza da togliere in più sotto il punto reale più "
                         "basso (0 = base al fondo dell'oggetto)")
    ap.add_argument("--orient", default="",
                    help="rotazioni di 90° dopo l'orientamento automatico, in ordine: x = in "
                         "avanti, y = di lato, z = sul piatto (es. 'xz')")
    ap.add_argument("--turn", type=float, default=0,
                    help="rotazione in gradi attorno alla verticale (per girare il davanti)")
    ap.add_argument("--smooth", type=float, default=0.3,
                    help="levigatura della superficie da 0 (nessuna) a 1 (forte)")
    ap.add_argument("--crop", type=float, default=0.6,
                    help="raggio del ritaglio attorno alla testa, in multipli della distanza "
                         "delle camere (0 = nessun ritaglio)")
    ap.add_argument("--up", default="auto", choices=["auto", *UP_VECTORS],
                    help="asse 'su' della ricostruzione (auto = stimato dalle camere)")
    ap.add_argument("--quality", choices=QUALITY, default="media")
    ap.add_argument("--colmap", default=None, help="percorso eseguibile COLMAP")
    ap.add_argument("--openmvs-dir", default=None, help="cartella eseguibili OpenMVS")
    ap.add_argument("--bambu", default=None, help="percorso eseguibile Bambu Studio")
    ap.add_argument("--from-step", type=int, default=1,
                    help="riparti da uno step (1 immagini, 2 colmap, 4 openmvs, 5 pulizia)")
    ap.add_argument("--no-open", action="store_true", help="non aprire Bambu Studio")
    ap.add_argument("--mask", action="store_true",
                    help="separa il soggetto dallo sfondo nelle foto (rembg) e toglie i punti "
                         "di sfondo; da evitare per busti a muro e rilievi")
    ap.add_argument("--marker-mm", type=float, default=0,
                    help="lato in mm di un marker ArUco 4x4 nelle foto: scala reale invece "
                         "di --height")
    ap.add_argument("--mirror", default="", choices=["", "auto", "destra", "sinistra"],
                    help="specchia una metà sull'altra (auto = la metà più pulita; destra/"
                         "sinistra come la vede chi guarda il davanti)")
    ap.add_argument("--threads", type=int, default=0,
                    help="core della CPU da usare (0 = tutti)")
    ap.add_argument("--ai-fill", action="store_true",
                    help="l'IA (TripoSR) riempie solo le parti che le foto non hanno visto")
    ap.add_argument("--no-texture", action="store_true",
                    help="niente texture dalle foto nel GLB/USDZ")
    ap.add_argument("--ai", action="store_true",
                    help="usa l'IA (immagina le parti non viste) invece della fotogrammetria")
    a = ap.parse_args(argv)
    for name in ("images", "video", "out", "colmap", "openmvs_dir", "bambu"):
        setattr(a, name, clean_path(getattr(a, name)))
    if a.height <= 0:
        ap.error("--height deve essere maggiore di 0")
    if not 0 <= a.cut < 0.9:
        ap.error("--cut deve essere tra 0 e 0.9")
    if a.fps < 0:
        ap.error("--fps non può essere negativo (0 = automatico)")
    if not 0 <= a.smooth <= 1:
        ap.error("--smooth deve essere tra 0 e 1")
    if a.crop < 0:
        ap.error("--crop non può essere negativo")
    if a.max_frames < 10:
        ap.error("--max-frames deve essere almeno 10")
    if a.orient.strip("xyz"):
        ap.error("--orient accetta solo le lettere x, y, z")
    if not 1 <= a.from_step <= 5:
        ap.error("--from-step deve essere tra 1 e 5")

    max_size, res_level, poisson_depth, faces = QUALITY[a.quality]
    limit_cpu(a.threads)
    work = Path(a.out).resolve()
    work.mkdir(parents=True, exist_ok=True)
    state = work / "stato.json"
    t0 = time.time()

    if a.from_step <= 1:
        if a.video and not Path(a.video).is_file():
            sys.exit(f"Video non trovato: {a.video}")
        if a.images and not Path(a.images).is_dir():
            sys.exit(f"Cartella di foto non trovata: {a.images}")
        single_camera = prepare_images(a, work, max_size)
        state.write_text(json.dumps({"single_camera": single_camera}))
        (work / "ia.ply").unlink(missing_ok=True)
    cuts_file = work / "tagli.json"
    if a.from_step <= 4 and cuts_file.is_file():
        # una nuova ricostruzione ha un altro sistema di coordinate: i tagli vecchi
        # finirebbero nel posto sbagliato, quindi vanno rifatti sul modello nuovo
        os.replace(cuts_file, work / "tagli_precedenti.json")
        warn("Ricostruzione rifatta da capo: i tagli a mano vanno rifatti sul nuovo modello.")
    use_ai = a.ai or (a.from_step <= 3 and 0 < count_images(work / "images") < 3)
    if not use_ai and a.from_step <= 4:
        try:
            colmap_and_openmvs(a, work, state, max_size, res_level)
        except SystemExit as e:
            if a.from_step > 3 or not count_images(work / "images") or not ai_available():
                raise
            warn(f"{e} Provo con l'IA, che immagina le parti non viste.")
            use_ai = True
    if use_ai:
        if not ai_available():
            sys.exit(AI_MISSING)
        if not count_images(work / "images"):
            sys.exit("Nessuna immagine preparata: esegui la pipeline completa (Avvia tutto).")
        run_ai(work)

    raw = latest_input(work)
    if a.mask and raw and raw.name != "ia.ply":
        PROGRESS.start("surface")
        import extras
        extras.make_masks(work)
    if not raw:
        sys.exit(f"Nessuna nuvola densa in {work / 'mvs'}: esegui prima la pipeline completa.")
    final = clean_and_prepare(raw, work, a.up, a.height, a.cut, a.crop, poisson_depth, faces,
                              a.smooth, a.turn, a.orient, a.mask, a.marker_mm,
                              not a.no_texture, res_level, a.mirror, a.ai_fill)
    if not use_ai and a.from_step <= 4 and count_images(work / "images") and looks_broken(work) \
            and not ai_available():
        warn("La ricostruzione fotografica è venuta incompleta (soggetto che si muove, "
             "superficie senza dettagli o foto non agganciate): rifai le foto con più "
             "luce, girando lentamente attorno al soggetto fermo.")
    elif not use_ai and a.from_step <= 4 and count_images(work / "images") and looks_broken(work):
        warn("La ricostruzione fotografica è venuta incompleta (soggetto che si muove, "
             "superficie senza dettagli o foto non agganciate): provo con l'IA.")
        run_ai(work)
        final = clean_and_prepare(latest_input(work), work, a.up, a.height, a.cut, a.crop,
                                  poisson_depth, faces, a.smooth, a.turn, a.orient,
                                  a.mask, a.marker_mm, not a.no_texture, res_level, a.mirror, a.ai_fill)
    mins, secs = divmod(int(time.time() - t0), 60)
    print(f"Tempo totale: {mins} min {secs} s")
    emit("DONE", final)
    if not a.no_open:
        open_in_bambu(final, a.bambu)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
