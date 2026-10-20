#!/usr/bin/env python3
"""Effigie: interfaccia grafica (Tkinter) per bust_pipeline.py.

A sinistra si sceglie cosa ricostruire (foto o video, anche trascinandoli nella finestra),
al centro il modello 3D da girare con il mouse, a destra le rifiniture che si aggiornano
da sole e i pulsanti per la stampa. Ogni modello ha la sua cartella in progetti/.
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

import ui_kit as ui
from monitor import Monitor, gb
from bust_pipeline import IMAGE_EXTS, clean_path, latest_input, open_in_bambu, video_duration
from ui_kit import ACCENT, BG, CARD, ERR, FAINT, FG, FIELD, LINE, MUTED, OK, SIDE, WARN
from viewer3d import Viewer, has_colors, model_file

ROOT = Path(__file__).resolve().parent.parent      # cartella del progetto (src/ è il codice)
SRC = Path(__file__).resolve().parent
CFG = (Path.home() / "Documents" / "Effigie" / "config.json"
       if (ROOT / "python" / "python.exe").exists() else ROOT / "config.json")
APP_NAME = "Effigie"
VERSION = "1.0"
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".3gp", ".mts"}
BLENDER_DIRS = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"]
STEPS = ["Foto", "Camere", "Nuvola 3D", "Superficie", "Pronto"]
STAGE_STEP = {"images": 0, "features": 1, "matching": 1, "mapper": 1, "undistort": 1,
              "densify": 2, "ai": 2, "surface": 3, "texture": 3}
# minuti ≈ a + b × numero di immagini (misurati su Ryzen 5 7600, solo CPU)
QUALITY = {"bozza": ("Bozza", "veloce", 1.0, 0.07), "media": ("Media", "consigliata", 2.0, 0.16),
           "alta": ("Alta", "più dettagli", 3.0, 0.45)}
CFG_VERSION = 3


# ----------------------------------------------------------------- funzioni di servizio
def load_cfg(path=None):
    path = path or CFG
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def check_source(mode, src):
    """Messaggio d'errore se la sorgente non è valida, altrimenti None."""
    src = clean_path(src)
    if not src:
        return "Scegli un video o una cartella di foto."
    p = Path(src)
    if mode == "video" and not p.is_file():
        return f"Video non trovato:\n{src}"
    if mode == "images" and not p.is_dir():
        return f"Cartella di foto non trovata:\n{src}"
    return None


def python_exe():
    """Python del progetto (.venv, con tutte le librerie) anche se l'interfaccia
    è stata avviata con il Python di sistema."""
    for p in (ROOT / "python" / "python.exe", ROOT / ".venv" / "Scripts" / "python.exe",
              ROOT / ".venv" / "bin" / "python"):
        if p.exists():                       # python/ = pacchetto di installazione
            return str(p)
    return sys.executable


INSTALLED = (ROOT / "python" / "python.exe").exists()
# installato: la cartella del programma può non essere scrivibile, i dati vanno in Documenti
DATA = Path.home() / "Documents" / "Effigie" if INSTALLED else ROOT
EXAMPLES = ROOT / "Esempi"


def build_cmd(mode, src, out, height, cut, quality, from_step, bambu="", crop=0.6,
              smooth=0.3, ai=False, orient="", fps=0.0, up="auto", mirror="", threads=0,
              colors=True):
    cmd = [python_exe(), "-u", str(SRC / "bust_pipeline.py"),
           "--images" if mode == "images" else "--video", src or ".",
           "--out", str(out), "--height", f"{height:.1f}",
           "--cut", f"{cut:.3f}", "--crop", f"{crop:.2f}", "--smooth", f"{smooth:.2f}",
           f"--up={up}",                          # "=": altrimenti "-y" sembra un'opzione
           "--quality", quality, "--fps", f"{fps:.2f}", "--from-step", str(from_step),
           "--no-open"]
    if from_step == 5 or not colors:
        cmd.append("--no-texture")           # ritocchi: pochi secondi (la texture solo da zero)
    if orient:
        cmd.append(f"--orient={orient}")
    if mirror:
        cmd += ["--mirror", mirror]
    if threads:
        cmd += ["--threads", str(threads)]
    if ai:
        cmd.append("--ai")
    if bambu:
        cmd += ["--bambu", bambu]
    return cmd


def kill_tree(proc):
    """Termina il processo e i figli (COLMAP/OpenMVS continuerebbero altrimenti)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if proc.poll() is None:
        proc.kill()


def find_blender():
    for base in BLENDER_DIRS:
        hits = sorted(base.glob("Blender*/blender.exe"), reverse=True) if base.is_dir() else []
        if hits:
            return str(hits[0])
    return shutil.which("blender")


def final_model(out):
    """Il modello da stampare: quello scolpito in Blender se più recente."""
    out = Path(out)
    tmf, sculpt = out / "busto.3mf", out / "scolpito.stl"
    candidates = [p for p in (tmf, out / "busto.stl", sculpt) if p.is_file()]
    if not candidates:
        return None
    if sculpt.is_file() and tmf.is_file() and sculpt.stat().st_mtime > tmf.stat().st_mtime:
        return sculpt
    return candidates[0]


def dpi_aware():
    """Finestra nitida sugli schermi con zoom di Windows (125%, 150%...)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def slug(name):
    s = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_")
    return s[:40] or "modello"


def project_stamp(d):
    return max((p.stat().st_mtime for p in (d / "progetto.json", d / "busto.json") if p.is_file()),
               default=0.0)


def list_projects(base, extra=()):
    """Cartelle dei modelli, la più recente per prima."""
    dirs = []
    if base.is_dir():
        dirs = [d for d in base.iterdir() if d.is_dir() and
                ((d / "progetto.json").is_file() or (d / "busto.stl").is_file())]
    seen = {d.resolve() for d in dirs}
    for d in map(Path, extra):
        if d.is_dir() and (d / "busto.stl").is_file() and d.resolve() not in seen:
            dirs.append(d)
            seen.add(d.resolve())
    return sorted(dirs, key=project_stamp, reverse=True)


def project_name(d):
    return read_json(Path(d) / "progetto.json").get("name") or Path(d).name


def list_photos(folder):
    try:
        return sorted(f for f in Path(folder).iterdir()
                      if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
    except OSError:
        return []


def classify_drop(paths):
    """(modo, percorso) da file/cartelle trascinati, oppure (None, messaggio)."""
    paths = [Path(p) for p in paths]
    folders = [p for p in paths if p.is_dir()]
    videos = [p for p in paths if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    photos = [p for p in paths if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if folders:
        return "images", folders[0]
    if videos:
        return "video", videos[0]
    if photos:
        return "images", photos[0].parent          # le foto trascinate: tutta la loro cartella
    return None, "Trascina una cartella di foto o un video (mp4, mov...)."


def simplify_orient(seq):
    """Toglie i giri completi: quattro rotazioni uguali di fila non cambiano niente."""
    while any(ax * 4 in seq for ax in "xyz"):
        for ax in "xyz":
            seq = seq.replace(ax * 4, "")
    return seq[-24:]


def estimate_minutes(quality, n_images):
    _, _, a, b = QUALITY[quality]
    return a + b * n_images


def video_thumb(video, size=256):
    """Un fotogramma del video (per la miniatura), in una cartella temporanea."""
    from bust_pipeline import find_exe
    key = hashlib.md5(f"{video}{Path(video).stat().st_mtime}".encode()).hexdigest()[:12]
    out = Path(tempfile.gettempdir()) / f"busto3d_{key}.jpg"
    if not out.is_file():
        try:
            ffmpeg = find_exe("ffmpeg")
        except SystemExit:
            return None
        dur = video_duration(video) or 0
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss",
                        f"{dur * 0.4:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={size}:-2", str(out)], capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out if out.is_file() else None


# ----------------------------------------------------------------- applicazione
class App(tk.Tk):
    def __init__(self):
        dpi_aware()
        super().__init__()
        self.withdraw()                      # costruita nascosta: niente sfarfallio all'avvio
        self.k = max(1.0, self.winfo_fpixels("1i") / 96)
        self.splash_t0 = time.time()
        splash_img = ROOT / "assets" / "splash.png"
        self.splash = (ui.Splash(self, splash_img, f"versione {VERSION}")
                       if splash_img.is_file() else None)
        self.splash_step(15)
        self.t = ui.Theme(self, self.k)
        px = self.t.px
        self.title(APP_NAME)
        self.configure(bg=BG)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(px(1280), int(sw * 0.94)), min(px(820), int(sh * 0.88))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - px(16))}")
        self.minsize(min(px(1020), w), min(px(640), h))

        self.proc = None
        self.q = queue.Queue()
        self.t_start = None
        self.eta = None
        self.stage_key = ""
        self.stage_label = ""
        self.auto_job = None
        self.auto_pending = False
        self.last_warning = None
        self.last_error = None
        self.run_step = 1
        self.run_log = None
        self.loading_project = False
        self.source = None                   # (modo, percorso) scelto per il prossimo modello
        self.work = None                     # cartella del progetto mostrato
        self.model_info = None
        self.orient_changed = False

        self.splash_step(30)
        cfg = load_cfg()
        self.cfg = cfg
        legacy = []
        if cfg.get("versione", 1) < CFG_VERSION and cfg.get("out"):
            legacy.append(cfg["out"])        # vecchia cartella unica "lavoro": resta visibile
        self.legacy = cfg.get("legacy", []) + [x for x in legacy if x not in cfg.get("legacy", [])]
        self.projects_dir = Path(cfg.get("projects_dir") or DATA / "progetti")
        self.height_mm = tk.DoubleVar(value=cfg.get("height", 120))
        self.quality = tk.StringVar(value=cfg.get("quality", "media")
                                    if cfg.get("quality") in QUALITY else "media")
        self.ai = tk.BooleanVar(value=False)
        self.want_colors = tk.BooleanVar(value=cfg.get("colori", True))
        self.smooth = tk.DoubleVar(value=0.3)
        self.mirror = tk.StringVar(value="")
        self.cut = tk.DoubleVar(value=0.0)
        self.crop = tk.DoubleVar(value=0.6)
        self.orient_seq = ""                 # rotazioni di 90° scelte dall'utente
        self.bambu = tk.StringVar(value=cfg.get("bambu", ""))
        self.auto = tk.BooleanVar(value=cfg.get("auto", True))
        self.sound = tk.BooleanVar(value=cfg.get("sound", True))
        self.fps_auto = tk.BooleanVar(value=cfg.get("fps_auto", True))
        self.fps = tk.DoubleVar(value=cfg.get("fps", 3.0))
        self.cpu_pct = tk.IntVar(value=cfg.get("cpu", 80))
        self.status = tk.StringVar()
        self.timer = tk.StringVar()
        self.tab = tk.StringVar(value="model")

        self.splash_step(45)
        self.monitor = Monitor(str(self.projects_dir if self.projects_dir.exists() else ROOT))
        self.res_win = None
        self.splash_step(60)
        self.build()
        self.splash_step(85)
        ui.app_icon(self, self.t)
        for var in (self.height_mm, self.smooth, self.cut, self.crop):
            var.trace_add("write", lambda *_: self.schedule_auto())
        self.quality.trace_add("write", lambda *_: self.update_estimate())
        self.mirror.trace_add("write", lambda *_: self.mirror_changed())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_keys()
        self.splash_step(92)
        # la finestra si prepara invisibile (trasparenza 0): compare solo dopo lo splash
        self.attributes("-alpha", 0.0)
        self.deiconify()
        ui.dark_titlebar(self)

        self.update()
        self.drop_ok = ui.enable_file_drop(self, self.on_drop)
        self.after(100, self.poll)
        self.new_project()                     # all'avvio area di lavoro vuota
        self.close_splash()

    def close_splash(self, min_time=2.5):
        """Dopo almeno min_time secondi: barra al 100%, poi lo splash sale e svanisce mentre
        la finestra principale compare in dissolvenza."""
        sp = getattr(self, "splash", None)
        if not sp:
            self.attributes("-alpha", 1.0)
            return
        left = min_time - (time.time() - self.splash_t0)
        if left > 0:
            for i in range(1, 11):
                sp.step(92 + 0.8 * i)
                self.after(int(left * 100))
        sp.step(100)
        self.after(250)
        x0, y0 = sp.winfo_x(), sp.winfo_y()
        frames = 22
        for i in range(1, frames + 1):
            t = i / frames
            ease = 1 - (1 - t) ** 3                       # rallenta alla fine
            sp.attributes("-alpha", 1 - ease)
            sp.geometry(f"+{x0}+{int(y0 - 24 * ease)}")
            self.attributes("-alpha", min(1.0, ease * 1.15))
            self.update()
            self.after(14)
        sp.destroy()
        self.splash = None
        self.attributes("-alpha", 1.0)
        self.lift()
        self.focus_force()

    def splash_step(self, pct):
        if getattr(self, "splash", None):
            self.splash.step(pct)

    # ------------------------------------------------------------ costruzione
    def build_menubar(self):
        def file_items():
            items = [("Nuovo modello", self.new_project, "Ctrl+N"),
                     ("Apri progetto…", self.browse_project, "Ctrl+O"), None,
                     ("PROGETTI RECENTI", None)]
            projects = list_projects(self.projects_dir, self.legacy)[:8]
            if not projects:
                items.append(("   Ancora nessun modello", None))
            for d in projects:
                stamp = time.strftime("%d/%m %H:%M", time.localtime(project_stamp(d)))
                items.append((f"   {project_name(d)}",
                              None if self.running() else (lambda d=d: self.open_project(d)),
                              stamp))
            examples = sorted(d for d in EXAMPLES.iterdir() if d.is_dir()) \
                if EXAMPLES.is_dir() else []
            if examples:
                items += [None, ("ESEMPI", None)]
                items += [(f"   {project_name(d)}",
                           None if self.running() else (lambda d=d: self.open_project(d)))
                          for d in examples]
            return items + [None,
                            ("Esporta il modello…", self.export),
                            ("Apri la cartella del modello", self.open_folder),
                            ("Apri la cartella dei progetti", self.open_projects_dir), None,
                            ("Impostazioni…", self.open_settings), ("Esci", self.on_close)]
        menus = [("File", file_items),
                 ("Strumenti", [("Apri in Bambu Studio", self.open_bambu),
                                ("Scolpisci in Blender…", self.sculpt),
                                ("Risorse del computer", self.open_resources)]),
                 ("Aiuto", [("Guida", self.open_help)])]
        self.menubar = ui.DarkMenuBar(self, self.t, menus)
        self.menubar.pack(fill="x")
        tk.Frame(self, bg=LINE, height=1).pack(fill="x")
        self.bind_all("<Control-n>", lambda e: self.new_project())
        self.bind_all("<Control-o>", lambda e: self.browse_project())

    def browse_project(self):
        """Apre un progetto già elaborato da qualunque cartella (anche fuori da progetti)."""
        if self.running():
            return
        p = filedialog.askdirectory(title="Cartella di un progetto già elaborato",
                                    initialdir=str(self.projects_dir))
        if not p:
            return
        d = Path(p)
        if not any((d / f).is_file() for f in ("progetto.json", "busto.stl", "busto.3mf")):
            return messagebox.showerror("Apri progetto", "In questa cartella non c'è un "
                                        "modello elaborato (manca busto.stl).")
        inside = d.resolve().parent == self.projects_dir.resolve()
        if not inside and all(Path(x).resolve() != d.resolve() for x in self.legacy):
            self.legacy.append(str(d))              # resta nei recenti anche dopo il riavvio
        self.open_project(d)

    def build(self):
        px = self.t.px
        self.build_menubar()
        head = ttk.Frame(self, padding=(px(10), px(5), px(10), px(5)))
        head.pack(fill="x")
        self.logo = ui.ImageTk.PhotoImage(ui.symbol(px(22)))
        tk.Label(head, image=self.logo, bg=BG).pack(side="left")
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left",
                                                                    padx=(px(8), px(20)))
        self.proj_label = tk.StringVar(value="Nuovo modello")
        self.proj_title = tk.StringVar()
        self.proj_label.trace_add("write", lambda *_: self.proj_title.set(
            f"{self.proj_label.get()}   ▾"))
        self.proj_btn = ttk.Menubutton(head, textvariable=self.proj_title, style="Pill.TMenubutton")
        self.proj_menu = tk.Menu(self.proj_btn, tearoff=False, bg=CARD, fg=FG, bd=0,
                                 activebackground=ACCENT, activeforeground="white",
                                 font=self.t.f["base"], postcommand=self.fill_project_menu)
        self.proj_btn["menu"] = self.proj_menu
        self.proj_btn.pack(side="left")
        ui.Tooltip(self.proj_btn, "I modelli già creati: sceglierne uno per rivederlo.", self.t)
        self.btn_new = ttk.Button(head, text="＋  Nuovo", style="Ghost.TButton",
                                  command=self.new_project)
        self.btn_new.pack(side="left", padx=px(6))
        ttk.Button(head, text="⚙  Impostazioni", style="Ghost.TButton",
                   command=self.open_settings).pack(side="right")
        self.ic_chart = ui.ImageTk.PhotoImage(ui.glyph("chart", px(16), ui.MUTED, BG))
        ttk.Button(head, text=" Risorse", image=self.ic_chart, compound="left",
                   style="Ghost.TButton", command=self.open_resources).pack(side="right",
                                                                            padx=px(4))
        ttk.Button(head, text="?  Come si fa", style="Ghost.TButton",
                   command=self.open_help).pack(side="right", padx=px(4))

        body = ttk.Frame(self, padding=(px(8), px(4), px(8), px(6)))
        body.pack(fill="both", expand=True)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)
        self.toolbar = None
        self.build_sidebar(body).grid(row=0, column=1, sticky="ns")
        self.build_center(body).grid(row=0, column=2, sticky="nsew", padx=px(10))
        self.build_right(body).grid(row=0, column=3, sticky="ns")
        self.build_toolbar()

        bar = ttk.Frame(self, padding=(px(14), px(6), px(14), px(8)))
        bar.pack(fill="x")
        self.dot = tk.Label(bar, text="●", bg=BG, fg=FAINT, font=self.t.f["small"])
        self.dot.pack(side="left")
        ttk.Label(bar, textvariable=self.status, style="Muted.TLabel").pack(side="left",
                                                                          padx=(px(6), 0))
        self.meters = {}
        for key, name in (("gpu", "GPU"), ("ram", "RAM"), ("cpu", "CPU")):
            box = tk.Frame(bar, bg=BG, cursor="hand2")
            box.pack(side="right", padx=(px(14), 0))
            lab = tk.Label(box, text=name, bg=BG, fg=FAINT, font=self.t.f["tiny"], cursor="hand2")
            lab.pack(side="left")
            cv = tk.Canvas(box, width=px(38), height=px(6), bg=FIELD, highlightthickness=0,
                           bd=0, cursor="hand2")
            cv.pack(side="left", padx=px(5))
            val = tk.Label(box, text="–", bg=BG, fg=MUTED, font=self.t.f["tiny"], width=4,
                           anchor="w", cursor="hand2")
            val.pack(side="left")
            for w in (box, lab, cv, val):
                w.bind("<Button-1>", lambda e: self.open_resources())
            ui.Tooltip(box, "Uso del PC in questo momento: clic per i dettagli.", self.t)
            self.meters[key] = (cv, val)
        ttk.Label(bar, textvariable=self.timer, style="Muted.TLabel").pack(side="right",
                                                                           padx=(0, px(6)))
        self.after(1000, self.update_meters)

    def section(self, parent, number, title):
        """Titolo di sezione: numero e nome in maiuscolo, con una linea sottile."""
        px = self.t.px
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=(px(4), px(8)))
        ttk.Label(row, text=f"{number}  {title.upper()}", style="Card.Head.TLabel").pack(
            side="left")
        tk.Frame(row, bg=LINE, height=1).pack(side="left", fill="x", expand=True,
                                              padx=(px(8), 0))
        return row

    def build_sidebar(self, parent):
        px = self.t.px
        side = ttk.Frame(parent, style="Panel.TFrame", padding=px(12), width=px(290))
        side.pack_propagate(False)

        self.section(side, "1", "Foto o video")
        self.build_source(side)
        sw = ttk.Checkbutton(side, text="  Immagina le parti non viste (IA)", variable=self.ai,
                             style="Card.Switch.TCheckbutton")
        sw.pack(anchor="w", pady=(px(10), 0))
        cw = ttk.Checkbutton(side, text="  Colori dalle foto", variable=self.want_colors,
                             style="Card.Switch.TCheckbutton")
        cw.pack(anchor="w", pady=(px(6), 0))
        ui.Tooltip(cw, "Prende anche i colori: modello a colori (GLB, USDZ) e vista colorata "
                       "con ◐. Aggiunge circa un minuto.", self.t)
        ui.Tooltip(sw, "Con poche foto (anche una sola) l'intelligenza artificiale inventa "
                       "il retro. Risultato più approssimativo. Il programma la usa da solo "
                       "se la ricostruzione dalle foto non riesce.", self.t)
        ttk.Separator(side).pack(fill="x", pady=px(14))

        self.section(side, "2", "Altezza della stampa")
        row = ttk.Frame(side, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Scale(row, from_=30, to=300, variable=self.height_mm,
                  style="Card.Slim.Horizontal.TScale").pack(side="left", fill="x", expand=True)
        self.height_entry = ttk.Entry(row, width=5, style="Field.TEntry", justify="right",
                                      font=self.t.f["value"])
        self.height_entry.pack(side="left", padx=(px(10), px(4)))
        ttk.Label(row, text="mm", style="Card.Muted.TLabel").pack(side="left")
        self.height_entry.bind("<Return>", self.height_typed)
        self.height_entry.bind("<FocusOut>", self.height_typed)
        self.height_mm.trace_add("write", lambda *_: self.show_height())
        self.show_height()
        ttk.Separator(side).pack(fill="x", pady=px(14))

        self.section(side, "3", "Qualità")
        seg = ttk.Frame(side, style="Card.TFrame")
        seg.pack(fill="x")
        for i, (key, (name, sub, _, _)) in enumerate(QUALITY.items()):
            ttk.Radiobutton(seg, text=f"{name}\n{sub}", value=key, variable=self.quality,
                            style="Card.Seg.TRadiobutton").grid(row=0, column=i, sticky="nsew",
                                                                padx=(0 if i == 0 else px(6), 0))
            seg.columnconfigure(i, weight=1, uniform="q")
        self.estimate = ttk.Label(side, style="Card.Muted.TLabel")
        self.estimate.pack(anchor="w", pady=(px(8), 0))

        self.btn_run = ttk.Button(side, text="▶   Crea modello 3D", style="Card.Accent.TButton",
                                  command=self.run_or_stop)
        self.btn_run.pack(side="bottom", fill="x")
        self.run_hint = ttk.Label(side, style="Card.Faint.TLabel", justify="center",
                                  wraplength=px(290))
        self.run_hint.pack(side="bottom", pady=(0, px(8)))
        return side

    def build_source(self, parent):
        """Zona per scegliere o trascinare foto/video, e riepilogo di ciò che è stato scelto."""
        px = self.t.px
        k, r, dash, gap = self.t.k, 12, 7, 5
        drop = ui.DROP
        # bordo tratteggiato: i lati dritti dell'immagine si ripetono lungo il riquadro
        W = int(2 * px(r) + px(dash + gap))
        img = ui.Image.new("RGBA", (W * 4, W * 4), ui.hex_rgba(CARD))
        d = ui.ImageDraw.Draw(img)
        d.rounded_rectangle((2, 2, W * 4 - 3, W * 4 - 3), px(r) * 4, fill=ui.hex_rgba(drop),
                            outline=ui.hex_rgba("#4a4f5b"), width=max(4, int(1.5 * k * 4)))
        a, b = px(r) * 4 + px(dash) * 4, W * 4 - px(r) * 4
        lw = int(3 * k * 4)
        for box in ((a, 0, b - 1, lw), (a, W * 4 - 1 - lw, b - 1, W * 4 - 1),
                    (0, a, lw, b - 1), (W * 4 - 1 - lw, a, W * 4 - 1, b - 1)):
            d.rectangle(box, fill=ui.hex_rgba(drop))              # i buchi del tratteggio
        self.t.element("DropZone.bg", img.resize((W, W), ui.Image.LANCZOS), border=r)
        self.t.style.layout("DropZone.TFrame", [("DropZone.bg", {"sticky": "nsew"})])
        self.t.style.configure("DropZone.TFrame", background=drop)

        zone = ttk.Frame(parent, style="DropZone.TFrame", padding=(px(10), px(8)))
        zone.pack(fill="x")
        self.zone = zone
        # vuota
        self.zone_empty = ttk.Frame(zone, style="Drop.TFrame")
        self.drop_icon = ui.ImageTk.PhotoImage(ui.glyph("drop", px(26), MUTED, drop))
        tk.Label(self.zone_empty, image=self.drop_icon, bg=drop).grid(row=0, column=0, rowspan=2,
                                                                      padx=(0, px(10)))
        ttk.Label(self.zone_empty, text="Trascina qui foto o video",
                  style="Drop.TLabel").grid(row=0, column=1, sticky="w")
        row = ttk.Frame(self.zone_empty, style="Drop.TFrame")
        row.grid(row=1, column=1, sticky="w", pady=(px(4), 0))
        self.ic_photos = ui.ImageTk.PhotoImage(ui.glyph("photos", px(14), FG, FIELD))
        self.ic_video = ui.ImageTk.PhotoImage(ui.glyph("video", px(14), FG, FIELD))
        ttk.Button(row, text=" Foto", image=self.ic_photos, compound="left",
                   style="Drop.Small.TButton", command=self.pick_photos).pack(side="left")
        ttk.Button(row, text=" Video", image=self.ic_video, compound="left",
                   style="Drop.Small.TButton", command=self.pick_video).pack(side="left",
                                                                             padx=(px(6), 0))
        # piena
        self.zone_full = ttk.Frame(zone, style="Drop.TFrame")
        self.thumbs = ttk.Frame(self.zone_full, style="Drop.TFrame")
        self.thumbs.pack(fill="x")
        self.thumb_labels = []
        for i in range(4):
            lab = tk.Label(self.thumbs, bg=drop, bd=0)
            lab.grid(row=0, column=i, padx=(0 if i == 0 else px(6), 0))
            self.thumb_labels.append(lab)
        row = ttk.Frame(self.zone_full, style="Drop.TFrame")
        row.pack(fill="x", pady=(px(10), 0))
        self.src_name = ttk.Label(row, style="Drop.TLabel", font=self.t.f["value"])
        self.src_name.pack(side="left")
        change = ttk.Menubutton(row, text="Cambia ▾", style="Drop.Link.TMenubutton")
        menu = tk.Menu(change, tearoff=False, bg=CARD, fg=FG, activebackground=ACCENT,
                       activeforeground="white", bd=0, font=self.t.f["base"])
        menu.add_command(label="Cartella di foto…", command=self.pick_photos)
        menu.add_command(label="Video…", command=self.pick_video)
        change["menu"] = menu
        change.pack(side="right")
        self.src_detail = ttk.Label(self.zone_full, style="Drop.Muted.TLabel")
        self.src_detail.pack(anchor="w")
        self.src_badge = tk.Label(self.zone_full, bg=drop, font=self.t.f["small"], anchor="w")
        self.src_badge.pack(anchor="w", pady=(px(4), 0))
        self.zone_empty.pack(fill="x")

    def build_center(self, parent):
        px = self.t.px
        center = ttk.Frame(parent)
        top = ttk.Frame(center)
        top.pack(fill="x", pady=(0, px(6)))
        self.stepper = ui.Stepper(top, self.t, STEPS)
        self.stepper.pack(fill="x")
        self.bar = ttk.Progressbar(center, maximum=100, style="Bar.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(px(2), px(10)))

        tools = ttk.Frame(center)
        tools.pack(fill="x", pady=(0, px(6)))
        for i, (key, text) in enumerate((("model", "Modello 3D"), ("log", "Registro"))):
            ttk.Radiobutton(tools, text=text, value=key, variable=self.tab,
                            style="Seg.TRadiobutton", command=self.show_tab).pack(
                side="left", padx=(0, px(6)))
        self.t.style.configure("Seg.TRadiobutton", padding=(px(14), px(5)),
                               font=self.t.f["base"])

        self.stage = ttk.Frame(center)
        self.stage.pack(fill="both", expand=True)
        view_bg, log_bg = "#262626", "#1a1a1a"        # tela grigia neutra
        for name, fill in (("ViewerEmpty", view_bg), ("LogPanel", log_bg)):
            self.t.element(f"{name}.bg", self.t.rounded(30, 30, 12, fill, bg=BG), border=13)
            self.t.style.layout(f"{name}.TFrame", [(f"{name}.bg", {"sticky": "nsew"})])
            self.t.style.configure(f"{name}.TFrame", background=fill)
        self.viewer = Viewer(self.stage, bg=BG, panel=view_bg, radius=0,
                             placeholder_style="ViewerEmpty.TFrame", fg=MUTED,
                             font=self.t.f["base"])
        self.empty_icon = ui.ImageTk.PhotoImage(ui.symbol(px(64)))
        self.viewer.icon.config(image=self.empty_icon)
        self.viewer.on_load = self.model_loaded
        self.viewer.on_cut = self.model_cut
        self.viewer.on_sculpt = self.model_sculpted
        self.viewer.on_tool_end = self.touch_closed
        self.viewer.place(relx=0, rely=0, relwidth=1, relheight=1)
        ui.Tooltip(self.viewer.surface, "Trascina per girare · rotella per lo zoom · tasto "
                                        "destro per spostare · doppio clic per ricentrare",
                   self.t)
        logf = ttk.Frame(self.stage, style="LogPanel.TFrame", padding=px(8))
        self.log = tk.Text(logf, bg=log_bg, fg="#c9ccd3", insertbackground=FG, bd=0,
                           font=self.t.f["mono"], relief="flat", padx=px(12), pady=px(10),
                           wrap="word", highlightthickness=0)
        self.t.style.configure("Log.Vertical.TScrollbar", troughcolor=log_bg)
        scroll = ttk.Scrollbar(logf, command=self.log.yview, style="Log.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("cmd", foreground=FAINT)
        self.logf = logf

        info = ttk.Frame(center)
        info.pack(fill="x", pady=(px(10), px(2)), padx=px(4))
        self.info_dims = ttk.Label(info, style="Value.TLabel")
        self.info_dims.pack(side="left")
        self.info_more = ttk.Label(info, style="Muted.TLabel")
        self.info_more.pack(side="left", padx=px(12))
        self.info_ok = ttk.Label(info, style="Ok.TLabel")
        self.info_ok.pack(side="right")
        return center

    def build_toolbar(self):
        """Barra degli strumenti verticale (viste del modello), come nei programmi di grafica."""
        px = self.t.px
        bar = tk.Frame(self.stage, bg=CARD, highlightthickness=1,
                       highlightbackground="#3c3f47", padx=px(3), pady=px(5))
        self.toolbar = bar
        self.t.style.configure("Card.ToolIcon.TButton", padding=(0, px(5)), width=3,
                               font=(self.t.ui, 11))
        self.view_btns = []

        def tool(text, tip, command):
            b = ttk.Button(bar, text=text, style="Card.ToolIcon.TButton", command=command)
            b.pack(fill="x", pady=px(1))
            ui.Tooltip(b, tip, self.t)
            return b

        def gap():
            tk.Frame(bar, bg=LINE, height=1).pack(fill="x", pady=px(5), padx=px(4))
        for key, text, tip in (("front", "F", "Vista da davanti (1)"),
                               ("right", "D", "Vista da destra (2)"),
                               ("back", "R", "Vista da dietro (3)"),
                               ("left", "S", "Vista da sinistra (4)"),
                               ("top", "A", "Vista dall'alto (5)")):
            self.view_btns.append(tool(text, tip, lambda k=key: self.viewer.view(k)))
        gap()
        self.btn_cut = tool("✂", "Taglia: trascina una linea sul modello, poi clicca la parte "
                                 "da togliere (es. il piedistallo). Esc annulla.",
                            self.start_cut)
        self.btn_uncut = tool("↶", "Annulla l'ultimo taglio", self.undo_cut)
        self.btn_touch = tool("✎", "Ritocca a mano: leviga, erodi, base piatta e rotazione "
                                   "libera (R). Ctrl+Z annulla.", self.toggle_touch)
        gap()
        self.btn_colors = tool("◐", "Mostra il modello con i colori delle foto (C)",
                               self.toggle_colors)
        self.btn_quad = tool("⊞", "Quattro viste con le quote in mm: davanti, destra, alto e 3D "
                                  "(Q). Doppio clic su una vista per ingrandirla.",
                             self.toggle_quad)
        self.btn_spin = tool("↻", "Fa ruotare la vista (barra spaziatrice). Il modello non "
                                  "cambia.", self.toggle_spin)
        tool("⌖", "Ricentra la vista (doppio clic sul modello)", self.viewer.reset)
        tool("+", "Ingrandisci (rotella)", lambda: self.viewer.zoom(0.85))
        tool("−", "Rimpicciolisci (rotella)", lambda: self.viewer.zoom(1 / 0.85))
        gap()
        tool("☰", "Mostra il modello o il registro dell'elaborazione",
             lambda: (self.tab.set("log" if self.tab.get() == "model" else "model"),
                      self.show_tab()))

    # ------------------------------------------------------------ ritocchi a mano
    def build_touch_panel(self):
        """Pannello dei ritocchi, in alto a destra sopra il modello."""
        px = self.t.px
        box = tk.Frame(self.stage, bg=CARD, highlightthickness=1, highlightbackground="#3c3f47")
        inner = ttk.Frame(box, style="Card.TFrame", padding=(px(12), px(10)))
        inner.pack(fill="both")
        head = ttk.Frame(inner, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="Ritocca", style="Card.Head.TLabel").pack(side="left")
        ttk.Button(head, text="Fine", style="Card.Small.TButton",
                   command=self.toggle_touch).pack(side="right")
        self.touch_tool = tk.StringVar(value="smooth")
        row = ttk.Frame(inner, style="Card.TFrame")
        row.pack(fill="x", pady=(px(8), 0))
        for i, (value, text, tip) in enumerate((
                ("smooth", "Leviga", "Trascina sul modello: toglie grana e bozzi senza "
                                     "cambiare la forma."),
                ("erode", "Erodi", "Trascina sul modello: scava piano piano, come carta "
                                   "vetrata. Ogni passata toglie pochissimo."),
                ("base", "Base qui", "Clicca la zona che deve poggiare sul piatto: il modello "
                                     "si gira e quella zona diventa un fondo piatto."))):
            b = ttk.Radiobutton(row, text=text, value=value, variable=self.touch_tool,
                                style="Tight.Card.Seg.TRadiobutton",
                                command=lambda: self.viewer.start_tool(self.touch_tool.get()))
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(4), 0))
            row.columnconfigure(i, weight=1, uniform="t")
            ui.Tooltip(b, tip, self.t)
        self.touch_size = tk.DoubleVar(value=6.0)
        self.touch_strength = tk.DoubleVar(value=0.5)
        self.slider(inner, "Dimensione", self.touch_size, 1, 30, lambda v: f"{v:.0f} mm",
                    "Raggio del pennello in millimetri del modello stampato. Per «Base qui» "
                    "è la zona che diventa piatta.")
        self.slider(inner, "Intensità", self.touch_strength, 0.05, 1, lambda v: f"{v * 100:.0f}%",
                    "Quanto lavora ogni passata. Basso = più controllo.")
        for var in (self.touch_size, self.touch_strength):
            var.trace_add("write", lambda *_: self.touch_params())
        ttk.Label(inner, text="Ruota", style="Card.TLabel").pack(anchor="w", pady=(px(10), 0))
        row = ttk.Frame(inner, style="Card.TFrame")
        row.pack(fill="x", pady=(px(2), 0))
        self.touch_step = tk.IntVar(value=15)
        for i, (axis, name) in enumerate(((0, "X"), (1, "Y"), (2, "Z"))):
            for j, sign in enumerate((-1, 1)):
                ax = [0, 0, 0]
                ax[axis] = 1
                b = ttk.Button(row, text=f"{name}{'−' if sign < 0 else '+'}",
                               style="Tight.Card.Small.TButton",
                               command=lambda a=ax, s=sign: self.touch_rotate(a, s))
                b.grid(row=0, column=2 * i + j, sticky="ew", padx=(0 if i + j == 0 else px(3), 0))
                row.columnconfigure(2 * i + j, weight=1, uniform="r")
        steps = ttk.Frame(inner, style="Card.TFrame")
        steps.pack(fill="x", pady=(px(4), 0))
        ttk.Label(steps, text="Passo", style="Card.Faint.TLabel").pack(side="left")
        for v in (1, 5, 15, 45, 90):
            ttk.Radiobutton(steps, text=f"{v}°", value=v, variable=self.touch_step,
                            style="Tight.Card.Seg.TRadiobutton").pack(side="left", padx=(px(4), 0))
        row = ttk.Frame(inner, style="Card.TFrame")
        row.pack(fill="x", pady=(px(10), 0))
        ttk.Button(row, text="↶ Annulla", style="Card.Small.TButton",
                   command=self.touch_undo).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Torna all'originale", style="Card.Ghost.TButton",
                   command=self.touch_reset).pack(side="left", padx=(px(6), 0))
        ttk.Label(inner, text="Trascina sul modello per lavorare, fuori per girare la vista.",
                  style="Card.Faint.TLabel", wraplength=px(250), justify="left").pack(
            anchor="w", pady=(px(8), 0))
        self.touch_panel = box

    def touch_closed(self):
        if getattr(self, "touch_panel", None) is not None:
            self.touch_panel.place_forget()
        self.btn_touch.config(text="✎")

    def toggle_touch(self):
        panel = getattr(self, "touch_panel", None)
        if panel is not None and panel.winfo_ismapped():
            panel.place_forget()
            self.viewer.stop_tool()
            self.btn_touch.config(text="✎")
            return
        if self.running() or not self.work:
            return
        if panel is None:
            self.build_touch_panel()
        self.touch_params()
        if not self.viewer.start_tool(self.touch_tool.get()):
            return self.set_status("Per ritoccare serve il modello in 3D.", WARN)
        self.touch_panel.place(relx=1, x=-self.t.px(10), y=self.t.px(10), anchor="ne")
        tk.Misc.lift(self.touch_panel)
        self.btn_touch.config(text="■")
        self.set_status("Ritocco: trascina sul modello. Ctrl+Z annulla, Esc chiude.", ACCENT)

    def touch_params(self):
        try:
            self.viewer.brush_mm = float(self.touch_size.get())
            self.viewer.brush_strength = float(self.touch_strength.get())
        except (tk.TclError, AttributeError):
            pass
        self.viewer.redraw()

    def touch_rotate(self, axis, sign):
        self.viewer.sculpt_rotate(axis, sign * self.touch_step.get())

    def touch_undo(self):
        if not self.viewer.sculpt_undo():
            self.set_status("Niente da annullare.", FAINT)

    def touch_reset(self):
        """Via i ritocchi a mano: torna il modello creato dalle foto."""
        f = Path(self.work) / "scolpito.stl" if self.work else None
        if f and f.is_file():
            f.unlink()
        self.viewer.stop_tool()
        if getattr(self, "touch_panel", None) is not None:
            self.touch_panel.place_forget()
        self.btn_touch.config(text="✎")
        self.viewer.load(self.work, keep_view=True)
        self.set_status("Ritocchi tolti: è tornato il modello originale.", OK)

    def model_sculpted(self, sculptor, info):
        """Ogni ritocco si salva subito in scolpito.stl (va anche in Bambu Studio)."""
        if not self.work:
            return
        out = Path(self.work) / "scolpito.stl"
        try:
            sculptor.save(out)
        except OSError as e:
            return self.set_status(f"Ritocco non salvato: {e}", ERR)
        info["path"] = out
        self.model_info = info
        self.show_info()

    # ------------------------------------------------------------ tagli a mano
    def cuts_file(self):
        return Path(self.work) / "tagli.json" if self.work else None

    def start_cut(self):
        if self.viewer.tool:
            self.toggle_touch()
        if self.running() or not self.viewer.start_cut():
            return
        self.set_status("Trascina una linea sul modello, poi clicca la parte da togliere. "
                        "Esc per annullare.", ACCENT)

    def model_cut(self, n, p):
        """Piano scelto nel visualizzatore (mm del modello): salvato nelle coordinate della
        ricostruzione, poi il modello si rifà in pochi secondi."""
        if not self.work or self.running():
            return
        try:
            T = np.array(read_json(Path(self.work) / "busto.json")["transform"], float)
        except (KeyError, TypeError, ValueError):
            return self.set_status("Taglio non riuscito: rifai prima il modello.", ERR)
        inv = np.linalg.inv(T)
        n_raw = T[:3, :3].T @ n                   # le normali si trasformano con l'inversa trasposta
        p_raw = (inv @ np.append(p, 1))[:3]
        cuts = read_json(self.cuts_file()) if self.cuts_file().is_file() else []
        cuts = cuts if isinstance(cuts, list) else []
        cuts.append({"n": (n_raw / np.linalg.norm(n_raw)).tolist(), "p": p_raw.tolist()})
        write_json(self.cuts_file(), cuts)
        self.start(5, quiet=True)

    def undo_cut(self):
        f = self.cuts_file()
        cuts = read_json(f) if f and f.is_file() else []
        if not isinstance(cuts, list) or not cuts or self.running():
            return
        write_json(f, cuts[:-1])
        self.start(5, quiet=True)

    def toggle_colors(self):
        if not self.work or not has_colors(self.work):
            return self.set_status("Questo modello non ha i colori: rifallo con «Colori dalle "
                                   "foto» acceso.", WARN)
        on = not self.viewer.show_colors
        self.viewer.set_colors(on, self.work)
        self.btn_colors.config(text="●" if on else "◐")

    def place_toolbar(self, show):
        if not self.toolbar:
            return
        if show and self.tab.get() == "model":
            self.toolbar.place(x=self.t.px(10), rely=0.5, anchor="w")
            tk.Misc.lift(self.toolbar)
        else:
            self.toolbar.place_forget()

    def toggle_quad(self):
        self.viewer.set_quad(not self.viewer.quad)
        self.btn_quad.config(text="▣" if self.viewer.quad else "⊞")

    def collapsible(self, parent, title, first=False):
        """Pannello richiudibile: clic sul titolo per chiuderlo o riaprirlo."""
        px = self.t.px
        head = tk.Frame(parent, bg=SIDE, cursor="hand2")
        head.pack(fill="x", pady=(0 if first else px(8), 0))
        arrow = tk.Label(head, text="▾", bg=SIDE, fg=MUTED, font=self.t.f["small"],
                         cursor="hand2")
        arrow.pack(side="left", padx=(px(6), px(4)), pady=px(3))
        name = tk.Label(head, text=title.upper(), bg=SIDE, fg=MUTED, font=self.t.f["head"],
                        cursor="hand2")
        name.pack(side="left", pady=px(3))
        body = ttk.Frame(parent, style="Card.TFrame", padding=(px(2), px(8), px(2), 0))
        body.pack(fill="x")

        def toggle(_=None):
            if body.winfo_ismapped():
                body.pack_forget()
                arrow.config(text="▸")
            else:
                body.pack(fill="x", after=head)
                arrow.config(text="▾")
        for w in (head, arrow, name):
            w.bind("<Button-1>", toggle)
        return body

    def build_right(self, parent):
        px = self.t.px
        right = ttk.Frame(parent, style="Panel.TFrame", padding=px(12), width=px(260))
        right.pack_propagate(False)
        refine = self.collapsible(right, "Rifinitura", first=True)
        self.refine_hint = ttk.Label(refine, text="Cambia un valore: il modello si aggiorna da "
                                                 "solo in pochi secondi.",
                                     style="Card.Muted.TLabel", wraplength=px(255))
        self.refine_hint.pack(anchor="w", pady=(px(2), px(6)))
        self.slider(refine, "Levigatura", self.smooth, 0, 1, lambda v: f"{v * 100:.0f}%",
                    "Liscia la superficie. 0 = come ricostruita, con tutti i dettagli "
                    "(e il rumore).")
        self.slider(refine, "Taglio della base", self.cut, 0, 0.35, lambda v: f"{v * 100:.0f}%",
                    "La base piatta va già da sola sotto l'oggetto: questo toglie un pezzo "
                    "in più dal fondo (per esempio per accorciare un busto).")
        self.slider(refine, "Spazio attorno", self.crop, 0.3, 1.2, lambda v: f"{v:.2f}",
                    "Quanto tenere attorno al soggetto. Se manca un pezzo del soggetto "
                    "alzalo, se resta un pezzo di sfondo abbassalo.")

        orient = self.collapsible(right, "Orientamento")
        row = ttk.Frame(orient, style="Card.TFrame")
        row.pack(fill="x")
        self.orient_btns = []
        self.t.style.configure("Tight.Card.Small.TButton", padding=(px(2), px(6)),
                               font=self.t.f["small"])
        for i, (axis, text, tip) in enumerate((
                ("z", "↻ Gira", "Gira il modello sul piatto di 90° (per mettere il davanti "
                                "davanti)."),
                ("x", "⤵ Avanti", "Inclina il modello di 90° verso di te: per raddrizzarlo se "
                                  "è coricato, o metterlo in piedi se è sdraiato sul retro."),
                ("y", "⤷ Di lato", "Inclina il modello di 90° verso destra (se è coricato su "
                                   "un fianco)."))):
            b = ttk.Button(row, text=text, style="Tight.Card.Small.TButton",
                           command=lambda a=axis: self.orient(a))
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(6), 0))
            row.columnconfigure(i, weight=1, uniform="o")
            ui.Tooltip(b, tip, self.t)
            self.orient_btns.append(b)
        self.btn_reset = ttk.Button(orient, text="Orientamento automatico",
                                    style="Card.Ghost.TButton",
                                    command=lambda: self.orient(reset=True))
        self.btn_reset.pack(anchor="w", pady=(px(4), 0))
        self.orient_hint = ttk.Label(orient, style="Card.Faint.TLabel", wraplength=px(255),
                                     justify="left")
        self.orient_hint.pack(anchor="w", pady=(px(2), 0))

        sym = self.collapsible(right, "Simmetria")
        row = ttk.Frame(sym, style="Card.TFrame")
        row.pack(fill="x")
        self.mirror_btns = []
        self.t.style.configure("Tight.Card.Seg.TRadiobutton", padding=(px(1), px(5)))
        for i, (value, text, tip) in enumerate((
                ("", "No", "Il modello com'è stato ricostruito."),
                ("auto", "Auto", "Tiene la metà ricostruita meglio e la specchia sull'altra."),
                ("sinistra", "Sinistra", "Tiene la metà sinistra (guardando il davanti) e "
                                           "la specchia a destra."),
                ("destra", "Destra", "Tiene la metà destra (guardando il davanti) e la "
                                        "specchia a sinistra."))):
            b = ttk.Radiobutton(row, text=text, value=value, variable=self.mirror,
                                style="Tight.Card.Seg.TRadiobutton")
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(4), 0))
            row.columnconfigure(i, weight=1, uniform="m")
            ui.Tooltip(b, tip, self.t)
            self.mirror_btns.append(b)

        actions = ttk.Frame(right, style="Card.TFrame")
        actions.pack(side="bottom", fill="x")
        self.btn_bambu = ttk.Button(actions, text="Apri in Bambu Studio",
                                    style="Card.Primary.TButton", command=self.open_bambu)
        self.btn_bambu.pack(fill="x")
        self.btn_blender = ttk.Button(actions, text="Scolpisci in Blender…",
                                      style="Card.Small.TButton", command=self.sculpt)
        self.btn_blender.pack(fill="x", pady=(px(6), 0))
        row = ttk.Frame(actions, style="Card.TFrame")
        row.pack(fill="x", pady=(px(6), 0))
        self.btn_export = ttk.Button(row, text="Esporta…", style="Card.Small.TButton",
                                     command=self.export)
        self.btn_export.pack(side="left", fill="x", expand=True)
        self.btn_folder = ttk.Button(row, text="Cartella", style="Card.Small.TButton",
                                     command=self.open_folder)
        self.btn_folder.pack(side="left", fill="x", expand=True, padx=(px(6), 0))
        ttk.Label(right, text="Stampa", style="Card.Head.TLabel").pack(side="bottom", anchor="w",
                                                                       pady=(0, px(8)))
        ttk.Separator(right).pack(side="bottom", fill="x", pady=px(14))
        self.done_widgets = [self.btn_bambu, self.btn_blender, self.btn_export,
                             *self.orient_btns, self.btn_reset, *self.mirror_btns]
        return right

    def slider(self, parent, label, var, lo, hi, fmt, tip):
        px = self.t.px
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="x", pady=(px(8), 0))
        head = ttk.Frame(box, style="Card.TFrame")
        head.pack(fill="x")
        name = ttk.Label(head, text=label, style="Card.TLabel")
        name.pack(side="left")
        val = ttk.Label(head, style="Card.Value.TLabel")
        val.pack(side="right")
        scale = ttk.Scale(box, from_=lo, to=hi, variable=var, style="Card.Slim.Horizontal.TScale")
        scale.pack(fill="x", pady=(px(2), 0))
        for w in (name, scale):
            ui.Tooltip(w, tip, self.t)

        def show(*_):
            try:
                val.config(text=fmt(var.get()))
            except tk.TclError:
                pass
        var.trace_add("write", show)
        show()

    def bind_keys(self):
        self.bind("<Control-o>", lambda e: self.pick_photos())
        self.bind("<Control-O>", lambda e: self.pick_video())
        self.bind("<F5>", lambda e: self.run_or_stop() if not self.running() else None)
        self.bind("<Escape>", lambda e: self.stop() if self.running() else
                  self.toggle_touch() if self.viewer.tool else self.viewer.cancel_cut())
        self.bind("<Control-z>", lambda e: self.touch_undo() if self.viewer.tool else None)
        self.bind("r", lambda e: self.toggle_touch() if self.viewer_focus(e) else None)
        for key, name in (("1", "front"), ("2", "right"), ("3", "back"), ("4", "left"),
                          ("5", "top")):
            self.bind(key, lambda e, n=name: self.key_view(e, n))
        self.bind("<space>", lambda e: self.toggle_spin() if self.viewer_focus(e) else None)
        self.bind("q", lambda e: self.toggle_quad() if self.viewer_focus(e) else None)
        self.bind("c", lambda e: self.toggle_colors() if self.viewer_focus(e) else None)
        self.bind("<Home>", lambda e: self.viewer.reset())

    def viewer_focus(self, e):
        return not isinstance(e.widget, (tk.Entry, ttk.Entry, tk.Text))

    def key_view(self, e, name):
        if self.viewer_focus(e):
            self.viewer.view(name)

    # ------------------------------------------------------------ sorgente
    def pick_photos(self):
        p = filedialog.askdirectory(title="Cartella con le foto")
        if p:
            self.set_source("images", Path(p))

    def pick_video(self):
        p = filedialog.askopenfilename(title="Video", filetypes=[
            ("Video", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))), ("Tutti i file", "*.*")])
        if p:
            self.set_source("video", Path(p))

    def on_drop(self, paths):
        if self.running():
            return self.flash_status("Aspetta che finisca il modello in corso.", WARN)
        mode, path = classify_drop(paths)
        if not mode:
            return messagebox.showinfo(APP_NAME, path)
        self.set_source(mode, Path(path))

    def set_source(self, mode, path, quiet=False):
        """Sorgente del prossimo modello. Se c'è già un progetto con questa sorgente lo apre."""
        path = Path(clean_path(str(path)))
        if mode == "images" and not list_photos(path):
            sub = [d for d in path.iterdir() if d.is_dir() and list_photos(d)] if path.is_dir() else []
            if len(sub) == 1:
                path = sub[0]                      # es. archivio estratto con una sottocartella
            elif not quiet:
                return messagebox.showerror("Nessuna foto",
                                            f"Nella cartella non ci sono foto:\n{path}")
        self.source = (mode, path)
        if not quiet:
            existing = next((d for d in list_projects(self.projects_dir, self.legacy)
                             if read_json(d / "progetto.json").get("src") == str(path)), None)
            if existing and not self.running():
                self.open_project(existing, keep_source=True)
            elif not self.running():
                self.work = None
                self.proj_label.set(f"Nuovo: {slug(path.stem if mode == 'video' else path.name)}")
                self.viewer.clear("Premi «Crea modello 3D» per cominciare.")
                self.set_status("Pronto a creare il modello: scegli altezza e qualità, poi "
                                "«Crea modello 3D».")
                self.model_info = None
                self.show_info()
                self.stepper.set(-1)
                self.bar["value"] = 0
                self.enable_done(False)
        self.show_source()
        self.update_estimate()

    def show_source(self):
        px = self.t.px
        if not self.source:
            self.zone_full.pack_forget()
            self.zone_empty.pack(fill="x")
            return
        mode, path = self.source
        self.zone_empty.pack_forget()
        self.zone_full.pack(fill="x")
        name = path.name
        if len(name) > 24:
            name = name[:22] + "…"
        self.src_name.config(text=name)
        # segnaposto in pixel (width/height di una Label senza immagine sono in caratteri)
        if not hasattr(self, "thumb_blank"):
            self.thumb_blank = ui.ImageTk.PhotoImage(
                ui.Image.new("RGB", (px(60), px(60)), ui.hex_rgba("#33363e")[:3]))
        for lab in self.thumb_labels:
            lab.config(image=self.thumb_blank)
            lab.image = self.thumb_blank
        if not path.exists():
            self.n_images = 0
            self.src_detail.config(text="Cartella non trovata")
            self.src_badge.config(text="● Le foto originali sono state spostate o cancellate",
                                  fg=WARN)
            return
        if mode == "images":
            photos = list_photos(path)
            n = len(photos)
            self.n_images = n
            self.src_detail.config(text=f"{n} foto · {path.parent}" if len(str(path.parent)) < 30
                                   else f"{n} foto")
            if n >= 40:
                badge, color = "● Ottimo: tante foto", OK
            elif n >= 20:
                badge, color = "● Buono (40 o più è meglio)", OK
            elif n >= 3:
                badge, color = "● Poche foto: il retro potrebbe mancare", WARN
            else:
                badge, color = "● Troppo poche: userò l'IA", WARN
            self.src_badge.config(text=badge, fg=color)
            picks = [photos[i] for i in sorted({round(j * (n - 1) / 3) for j in range(4)})] if n else []
            self.load_thumbs(picks)
        else:
            self.n_images = 80
            self.src_detail.config(text="Video")
            self.src_badge.config(text="", fg=MUTED)
            self.load_thumbs([], video=path)

    def load_thumbs(self, photos, video=None):
        size, radius = self.t.px(60), self.t.px(8)
        token = object()
        self._thumb_token = token

        def job():
            imgs = []
            dur = None
            if video:
                dur = video_duration(video)
                frame = video_thumb(video)
                if frame:
                    imgs = [ui.thumbnail(frame, size, radius, ui.DROP)]
            else:
                imgs = [ui.thumbnail(p, size, radius, ui.DROP) for p in photos]
            self.after(0, lambda: self.thumbs_ready(token, imgs, dur, video))
        threading.Thread(target=job, daemon=True).start()

    def thumbs_ready(self, token, imgs, dur, video):
        if token is not self._thumb_token:
            return
        self.thumb_photos = [ui.ImageTk.PhotoImage(i) for i in imgs if i is not None]
        for lab, ph in zip(self.thumb_labels, self.thumb_photos + [None] * 4):
            if ph:
                lab.config(image=ph, width=0, height=0, bg=ui.DROP)
            else:
                lab.config(image="" if video else self.thumb_blank, bg=ui.DROP)
        if video:
            if dur:
                m, s = divmod(int(round(dur)), 60)
                self.src_detail.config(text=f"Video · {m}:{s:02d}")
                if dur >= 20:
                    self.src_badge.config(text="● Ottimo", fg=OK)
                elif dur >= 6:
                    self.src_badge.config(text="● Buono", fg=OK)
                else:
                    self.src_badge.config(text="● Molto corto: gira più piano", fg=WARN)
            else:
                self.src_detail.config(text="Video (durata non leggibile)")

    # ------------------------------------------------------------ progetti
    def fill_project_menu(self):
        m = self.proj_menu
        m.delete(0, "end")
        projects = list_projects(self.projects_dir, self.legacy)
        if not projects:
            m.add_command(label="Ancora nessun modello", state="disabled")
        for d in projects[:20]:
            stamp = time.strftime("%d/%m %H:%M", time.localtime(project_stamp(d)))
            mark = "●  " if self.work and Path(d).resolve() == Path(self.work).resolve() else "     "
            m.add_command(label=f"{mark}{project_name(d)}    {stamp}",
                          command=lambda d=d: self.open_project(d))
        m.add_separator()
        m.add_command(label="Apri la cartella dei progetti", command=self.open_projects_dir)
        if self.work:
            m.add_command(label="Elimina questo modello…", command=self.delete_project)
        state = "disabled" if self.running() else "normal"
        for i in range(m.index("end") + 1):
            if m.type(i) == "command" and state == "disabled":
                m.entryconfigure(i, state="disabled")

    def open_project(self, work, keep_source=False):
        work = Path(work)
        self.viewer.cancel_cut()                # un taglio in corso resta del modello di prima
        info = read_json(work / "progetto.json")
        self.loading_project = True                 # i valori cambiano: niente aggiornamento
        try:
            self.work = work
            self.proj_label.set(project_name(work))
            for var, key, default in ((self.smooth, "smooth", 0.3), (self.cut, "cut", 0.0),
                                      (self.crop, "crop", 0.6)):
                var.set(info.get(key, default))
            if "height" in info:
                self.height_mm.set(info["height"])
            if info.get("quality") in QUALITY:
                self.quality.set(info["quality"])
            self.orient_seq = info.get("orient", "")
            self.mirror.set(info.get("mirror", ""))
            if not keep_source and info.get("src"):
                src = Path(info["src"])
                if not src.is_absolute():            # esempi: foto dentro la cartella
                    src = work / src
                self.source = (info.get("mode", "images"), src)
                self.show_source()
        finally:
            self.loading_project = False
        self.last_params = self.refine_params()          # quelle del modello aperto
        self.cfg["last_project"] = str(work)
        if model_file(work):
            self.viewer.load(work)
            self.stepper.set(len(STEPS))
            self.bar["value"] = 100
            self.enable_done(True)
            self.set_status("Modello pronto. Giralo con il mouse; le rifiniture a destra "
                            "si applicano da sole.", OK)
        else:
            self.viewer.clear("Questo modello non è stato completato.\n"
                              "Premi «Crea modello 3D» per rifarlo.")
            self.stepper.set(-1)
            self.bar["value"] = 0
            self.enable_done(False)
            self.set_status("Modello incompleto.", WARN)
        self.update_estimate()

    def new_project(self):
        if self.running():
            return
        self.work = None
        self.mirror.set("")
        self.source = None
        self.proj_label.set("Nuovo modello")
        self.show_source()
        self.viewer.clear("Trascina qui sopra le foto o un video,\noppure usa i pulsanti a "
                          "sinistra.")
        self.model_info = None
        self.show_info()
        self.stepper.set(-1)
        self.bar["value"] = 0
        self.enable_done(False)
        self.set_status("Scegli le foto o il video del soggetto.")
        self.update_estimate()

    def project_for_source(self):
        """Cartella per il modello della sorgente scelta (stessa sorgente = stessa cartella)."""
        mode, path = self.source
        for d in list_projects(self.projects_dir, self.legacy):
            if read_json(d / "progetto.json").get("src") == str(path):
                return d
        base = slug(path.stem if mode == "video" else path.name)
        work = self.projects_dir / base
        n = 2
        while work.exists():
            work = self.projects_dir / f"{base}_{n}"
            n += 1
        return work

    def save_project(self, extra=None):
        if not self.work:
            return
        p = Path(self.work) / "progetto.json"
        data = read_json(p)
        data.update({"name": data.get("name") or Path(self.work).name,
                     "height": round(self.height_mm.get(), 1), "quality": self.quality.get(),
                     "smooth": round(self.smooth.get(), 2), "cut": round(self.cut.get(), 3),
                     "crop": round(self.crop.get(), 2), "orient": self.orient_seq,
                     "mirror": self.mirror.get()})
        if self.source and (extra or {}).get("new"):
            data.update({"src": str(self.source[1]), "mode": self.source[0],
                         "created": time.strftime("%Y-%m-%d %H:%M")})
        data.update({k: v for k, v in (extra or {}).items() if k != "new"})
        write_json(p, data)

    def delete_project(self):
        if not self.work or self.running():
            return
        work = Path(self.work)
        if not messagebox.askyesno("Elimina modello",
                                   f"Eliminare il modello «{project_name(work)}» e la sua "
                                   f"cartella?\n{work}\n\nLe foto e i video originali non "
                                   "vengono toccati.", icon="warning"):
            return
        try:
            shutil.rmtree(work)
        except OSError as e:
            return messagebox.showerror("Elimina modello", f"Non riesco a eliminarla:\n{e}")
        self.legacy = [x for x in self.legacy if Path(x).resolve() != work.resolve()]
        self.new_project()

    def open_projects_dir(self):
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.open_path(self.projects_dir)

    def show_height(self):
        try:
            v = self.height_mm.get()
        except tk.TclError:
            return
        if self.focus_get() is not self.height_entry:
            self.height_entry.delete(0, "end")
            self.height_entry.insert(0, f"{v:.0f}")

    def height_typed(self, _=None):
        try:
            v = float(self.height_entry.get().replace(",", "."))
        except ValueError:
            return self.show_height()
        v = min(max(v, 10), 400)
        if abs(v - self.height_mm.get()) > 0.05:
            self.height_mm.set(v)
        self.height_entry.delete(0, "end")
        self.height_entry.insert(0, f"{v:.0f}")

    def update_estimate(self):
        q = self.quality.get()
        if self.ai.get():
            text = "Con l'IA: circa 2 minuti."
        elif self.source and not Path(self.source[1]).exists():
            text = "Per rifarlo scegli di nuovo le foto con «Cambia»."
        elif self.source:
            n = getattr(self, "n_images", 50)
            mins = estimate_minutes(q, n)
            what = "dal video" if self.source[0] == "video" else f"con {n} foto"
            text = f"Circa {mins:.0f} minuti {what} (su questo PC)."
        else:
            text = {"bozza": "La più veloce, per provare.", "media": "Il giusto equilibrio.",
                    "alta": "Più dettagli, ma molto più lenta."}[q]
        self.estimate.config(text=text)
        self.update_run_button()

    def update_run_button(self):
        if self.running():
            return
        if self.source and not Path(self.source[1]).exists():
            self.btn_run.config(state="disabled")
            self.run_hint.config(text="Le foto di questo modello non ci sono più.")
        elif self.source:
            self.btn_run.config(state="normal")
            redo = self.work and model_file(self.work)
            self.run_hint.config(text="Rifà il modello da capo con le foto scelte." if redo
                                 else "Il PC resta usabile: il calcolo va a priorità bassa.")
        else:
            self.btn_run.config(state="disabled")
            self.run_hint.config(text="Prima scegli le foto o il video.")

    # ------------------------------------------------------------ esecuzione
    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def run_or_stop(self):
        if self.running():
            if self.run_step == 1 and not messagebox.askyesno(
                    "Ferma", "Fermare la creazione del modello?"):
                return
            return self.stop()
        self.start(1)

    def refine_params(self):
        """Le rifiniture che cambiano il modello: se non cambiano, niente da rifare."""
        try:
            return (round(self.height_mm.get(), 1), round(self.smooth.get(), 2),
                    round(self.cut.get(), 3), round(self.crop.get(), 2), self.mirror.get())
        except tk.TclError:
            return None

    def schedule_auto(self):
        """Dopo un cambio delle rifiniture rifà solo l'ultimo passaggio (pochi secondi).
        Una scrittura dello stesso valore (cursori che si ridisegnano, campo che perde il
        fuoco) non deve mai far ripartire l'elaborazione da sola."""
        if self.loading_project or not self.auto.get() or not self.work \
                or not latest_input(self.work):
            return
        if self.refine_params() == getattr(self, "last_params", None):
            return
        if self.auto_job:
            self.after_cancel(self.auto_job)
        self.auto_job = self.after(900, self.run_auto)

    def mirror_changed(self):
        if self.loading_project or not self.work or not latest_input(self.work):
            return
        if self.auto_job:
            self.after_cancel(self.auto_job)
        self.auto_job = None
        self.run_auto()

    def orient(self, axis="", reset=False):
        """Gira il modello di 90° (sul modello come si vede) o torna all'automatico."""
        if not self.work or not latest_input(self.work):
            return
        self.orient_seq = "" if reset else simplify_orient(self.orient_seq + axis)
        self.orient_changed = True
        if self.auto_job:
            self.after_cancel(self.auto_job)
        self.auto_job = None
        self.run_auto()

    def run_auto(self):
        self.auto_job = None
        self.last_params = self.refine_params()
        if self.running():
            self.auto_pending = True               # rifà appena finisce quello in corso
            return
        self.start(5, quiet=True)

    def start(self, from_step, quiet=False):
        try:
            height, cut = self.height_mm.get(), self.cut.get()
            crop, smooth, fps = self.crop.get(), self.smooth.get(), self.fps.get()
        except tk.TclError:
            return messagebox.showerror("Valori non validi", "Controlla i numeri inseriti.")
        self.bambu.set(clean_path(self.bambu.get()))
        if from_step == 1:
            if not self.source:
                return messagebox.showerror("Manca la sorgente", "Scegli le foto o il video.")
            mode, src = self.source
            if err := check_source(mode, str(src)):
                return messagebox.showerror("Sorgente non valida", err)
            self.work = self.project_for_source()
            Path(self.work).mkdir(parents=True, exist_ok=True)
            self.orient_seq = ""                       # nuovo modello: orientamento automatico
            self.proj_label.set(project_name(self.work))
            self.save_project({"new": True})
        elif not self.work or not latest_input(self.work):
            if not quiet:
                messagebox.showerror("Niente da rifinire", "Prima crea il modello.")
            return
        else:
            self.save_project()
            mode, src = self.source or ("images", Path("."))
        self.cfg["last_project"] = str(self.work)
        self.save_cfg()
        cmd = build_cmd(mode, str(src), self.work, height, cut, self.quality.get(), from_step,
                        self.bambu.get(), crop, smooth, self.ai.get() and from_step == 1,
                        self.orient_seq, 0.0 if self.fps_auto.get() else fps,
                        mirror=self.mirror.get(), threads=self.threads(),
                        colors=self.want_colors.get())
        self.run_step = from_step
        self.log.delete("1.0", "end")
        self.bar["value"] = 0 if from_step == 1 else 88
        self.stepper.set(0 if from_step == 1 else 3)
        self.t_start, self.eta = time.time(), None
        self.stage_key, self.stage_label = "", "Avvio" if from_step == 1 else "Aggiorno il modello"
        self.final = None
        self.last_warning = self.last_error = None
        self.set_running(True)
        if from_step == 1:
            self.viewer.clear("Sto creando il modello…\nI dettagli sono nella scheda «Registro».")
            self.viewer.show_loader("Creo il modello", 0)
            self.model_info = None
            self.show_info()
        try:
            self.run_log = open(Path(self.work) / "registro.txt", "w" if from_step == 1 else "a",
                                encoding="utf-8")
        except OSError:
            self.run_log = None
        flags = 0
        if sys.platform == "win32":                # PC usabile: calcolo a priorità bassa
            flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace",
                                         creationflags=flags, env=env)
        except OSError as e:
            self.set_running(False)
            return messagebox.showerror("Avvio non riuscito", str(e))
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()

    def reader(self, proc):
        for line in proc.stdout:
            self.q.put(line)
        self.q.put(("EXIT", proc.wait()))

    def poll(self):
        try:
            for _ in range(500):
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    self.finished(item[1])
                    continue
                self.handle_line(item)
        except queue.Empty:
            pass
        if self.running() and self.t_start:
            self.update_timer()
        self.after(150, self.poll)

    def handle_line(self, line):
        if self.run_log and not line.startswith("@@"):
            try:
                self.run_log.write(line)
            except OSError:
                pass
        if line.startswith("@@PROGRESS"):
            parts = line.split(maxsplit=3)
            if len(parts) >= 3:
                try:
                    self.bar["value"] = float(parts[1])
                except ValueError:
                    pass
                self.stage_key = parts[2]
                self.stage_label = parts[3].strip() if len(parts) > 3 else ""
                label = "Aggiorno" if self.run_step == 5 else "Creo il modello"
                self.viewer.show_loader(label, self.bar["value"])
                step = STAGE_STEP.get(self.stage_key)
                if step is not None and self.run_step == 1:
                    self.stepper.set(step)
        elif line.startswith("@@WARN"):
            msg = line[len("@@WARN"):].strip()
            self.last_warning = msg
            self.append_log(f"ATTENZIONE: {msg}\n", "warn")
            if self.run_log:
                self.run_log.write(f"ATTENZIONE: {msg}\n")
        elif line.startswith("@@DONE"):
            self.final = line[len("@@DONE"):].strip() or None
        elif not line.startswith("@@"):
            tag = None
            if line.startswith(">>"):
                tag = "cmd"
            elif "ERRORE" in line or "Errore" in line or "Traceback" in line:
                tag = "err"
            if line.strip():
                self.last_error = line.strip()
            self.append_log(line, tag)

    def append_log(self, text, tag=None):
        at_end = self.log.yview()[1] > 0.98
        self.log.insert("end", text, tag or ())
        if at_end:
            self.log.see("end")

    def update_timer(self):
        elapsed = time.time() - self.t_start
        m, s = divmod(int(elapsed), 60)
        text = f"{m}:{s:02d}"
        pct = float(self.bar["value"])
        if self.run_step == 1 and 12 <= pct < 99:
            remaining = elapsed * (100 - pct) / pct
            self.eta = remaining if self.eta is None else 0.85 * self.eta + 0.15 * remaining
            mins = max(1, round(self.eta / 60))
            text += f"  ·  ancora circa {mins} min"
        self.timer.set(text)
        self.set_status(f"{self.stage_label}…  {pct:.0f}%", ACCENT)

    def finished(self, code):
        self.set_running(False)
        if self.run_log:
            self.run_log.close()
            self.run_log = None
        elapsed = time.time() - (self.t_start or time.time())
        self.t_start = None
        if code == 0 and self.final:
            self.bar["value"] = 100
            self.stepper.set(len(STEPS))
            keep = self.run_step == 5 and not self.orient_changed
            self.orient_changed = False
            self.viewer.load(self.work, keep_view=keep)
            self.enable_done(True)
            m, s = divmod(int(elapsed), 60)
            self.timer.set(f"fatto in {m}:{s:02d}" if self.run_step == 1 else "")
            if self.last_warning:
                short = self.last_warning if len(self.last_warning) < 150 else \
                    self.last_warning[:147] + "…"
                self.set_status(f"Fatto, con un avviso: {short}", WARN)
            elif self.run_step == 1:
                self.set_status("Fatto! Giralo con il mouse, rifiniscilo a destra, poi «Apri "
                                "in Bambu Studio».", OK)
            else:
                self.set_status("Modello aggiornato.", OK)
            if self.run_step == 1:
                self.notify()
            self.save_project({"ok": True, "last_run": time.strftime("%Y-%m-%d %H:%M")})
        elif code:
            current = self.stepper.state[0]
            self.stepper.set(max(current, 0), failed=True)
            msg = self.last_error or "errore sconosciuto"
            if len(msg) > 160:
                msg = msg[:157] + "…"
            self.set_status(f"Non riuscito: {msg}", ERR)
            if self.run_step == 1:
                self.viewer.clear("Il modello non è riuscito.\nGuarda il «Registro» per i "
                                  "dettagli e i consigli.")
                self.notify()
            if self.run_step == 1 or not model_file(self.work):
                self.tab.set("log")
                self.show_tab()
        self.update_run_button()
        if self.auto_pending:
            self.auto_pending = False
            self.run_auto()

    def notify(self):
        if self.focus_displayof() is None:            # finestra in secondo piano
            ui.flash_taskbar(self)
        if self.sound.get() and sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            except (ImportError, RuntimeError):
                pass

    def stop(self):
        if self.running():
            kill_tree(self.proc)
            self.t_start = None
            self.timer.set("")
            self.set_status("Interrotto.", WARN)

    def set_running(self, running):
        px = self.t.px
        if running:
            if self.run_step == 1:
                self.btn_run.config(text="■   Ferma", style="Card.Danger.TButton",
                                    state="normal")
                self.run_hint.config(text="Puoi continuare a usare il PC: il calcolo va a "
                                          "priorità bassa.")
            else:
                self.btn_run.config(state="disabled")
            self.enable_done(False, keep_orient=self.run_step == 5)
            self.proj_btn.config(state="disabled")
            self.btn_new.config(state="disabled")
        else:
            self.btn_run.config(text="▶   Crea modello 3D", style="Card.Accent.TButton")
            self.proj_btn.config(state="normal")
            self.btn_new.config(state="normal")
            self.update_run_button()
        self.dot.config(fg=ACCENT if running else FAINT)
        # puntatore con clessidra mentre lavora (il PC resta usabile)
        self.config(cursor="starting" if running else "")
        if not running:
            self.viewer.hide_loader()
        del px

    def enable_done(self, on, keep_orient=False):
        st = "normal" if on else "disabled"
        if getattr(self, "toolbar", None):
            if on and not keep_orient:
                self.toolbar_on = True
                self.place_toolbar(True)
            elif not on and not keep_orient:
                self.toolbar_on = False
                self.place_toolbar(False)
                self.viewer.cancel_cut()
        for w in self.done_widgets:
            if keep_orient and (w in self.orient_btns or w is self.btn_reset):
                continue
            w.config(state=st)
        if on and not find_blender():
            self.btn_blender.config(state="disabled")
        self.btn_folder.config(state="normal" if self.work and Path(self.work).is_dir()
                               else "disabled")

    def set_status(self, text, color=None):
        self.status.set(text)
        self.dot.config(fg=color or FAINT)

    def flash_status(self, text, color):
        self.set_status(text, color)

    # ------------------------------------------------------------ vista
    def show_tab(self):
        if self.tab.get() == "log":
            self.viewer.place_forget()
            self.logf.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.place_toolbar(False)
        else:
            self.logf.place_forget()
            self.viewer.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.place_toolbar(getattr(self, "toolbar_on", False))
            self.viewer.redraw()

    def toggle_spin(self):
        on = self.viewer.toggle_spin()
        self.btn_spin.config(text="■" if on else "↻")

    def model_loaded(self, info):
        self.model_info = info
        self.show_info()

    def show_info(self):
        info = self.model_info
        if not info:
            for w in (self.info_dims, self.info_more, self.info_ok):
                w.config(text="")
            return
        x, y, z = info["extents"]
        self.info_dims.config(text=f"{x:.0f} × {y:.0f} × {z:.0f} mm")
        area, vol = info["area"], info["volume"]
        shell = area * 0.9                         # 2 perimetri e strati pieni sopra/sotto
        grams = 1.24 * (shell + 0.15 * max(vol - shell, 0)) / 1000
        extra = f"≈ {grams:.0f} g di PLA (riempimento 15%)"
        if Path(info["path"]).name == "scolpito.stl":
            extra += "  ·  ritoccato a mano"
        self.info_more.config(text=extra)
        meta = read_json(Path(self.work) / "busto.json") if self.work else {}
        if meta.get("flat_back") and not any(a in "xy" for a in self.orient_seq):
            why = "ripreso da un lato solo" if meta.get("relief") else "appeso a una parete"
            self.orient_hint.config(text=f"Retro piatto ({why}): è sdraiato sul retro, così si "
                                         "stampa senza supporti. «Avanti» lo mette in piedi.")
        else:
            self.orient_hint.config(text="")
        if meta.get("watertight", True):
            self.info_ok.config(text="✓ Chiuso e pronto per la stampa", style="Ok.TLabel")
        else:
            self.info_ok.config(text="Bambu Studio riparerà qualche buchino", style="Warn.TLabel")

    # ------------------------------------------------------------ azioni finali
    def open_bambu(self):
        model = final_model(self.work) if self.work else None
        if model:
            open_in_bambu(model, self.bambu.get() or None)
            self.set_status(f"Aperto in Bambu Studio: {model.name}", OK)

    def sculpt(self):
        blender = find_blender()
        out = Path(self.work) if self.work else None
        src = out / "busto.stl" if out else None
        if not blender or not src or not src.is_file():
            return messagebox.showerror("Blender", "Serve Blender (blender.org) e un modello creato.")
        messagebox.showinfo(
            "Scolpisci in Blender",
            "Si apre Blender in modalità Scultura con il pennello Smooth.\n\n"
            "• Pennelli a sinistra: Smooth (smussa), Draw (aggiunge), Flatten (appiattisce),\n"
            "  Grab (sposta), Crease (incide)\n"
            "• F = dimensione pennello, Shift+F = forza, Ctrl mentre dipingi = effetto inverso\n"
            "• Ctrl+S salva: il modello scolpito compare qui e va in Bambu Studio.")
        subprocess.Popen([blender, "--python", str(SRC / "blender_sculpt.py"), "--",
                          str(src), str(out / "scolpito.stl"), str(out / "scultura.blend")])
        self.watch_sculpt(out / "scolpito.stl")

    def watch_sculpt(self, path, last=None):
        """Quando Blender salva, il modello scolpito compare nel visualizzatore."""
        mtime = path.stat().st_mtime if path.is_file() else None
        if last is not None and mtime and mtime != last and self.work and \
                Path(self.work) == path.parent:
            self.viewer.load(self.work, keep_view=True)
            self.set_status("Modello scolpito aggiornato.", OK)
        if self.work and Path(self.work) == path.parent:
            self.after(2000, self.watch_sculpt, path, mtime if mtime else last or 0)

    def export(self):
        model = final_model(self.work) if self.work else None
        if not model:
            return
        ext = model.suffix
        name = f"{project_name(self.work)}{ext}"
        p = filedialog.asksaveasfilename(title="Esporta il modello", initialfile=name,
                                         defaultextension=ext,
                                         filetypes=[("Modello 3D", f"*{ext}"),
                                                    ("Tutti i file", "*.*")])
        if p:
            try:
                shutil.copyfile(model, p)
                self.set_status(f"Esportato: {p}", OK)
            except OSError as e:
                messagebox.showerror("Esporta", str(e))

    def open_folder(self):
        if self.work and Path(self.work).is_dir():
            self.open_path(Path(self.work))

    @staticmethod
    def open_path(p):
        if sys.platform == "win32":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", str(p)])

    # ------------------------------------------------------------ risorse del PC
    def update_meters(self):
        s = self.monitor.last
        for key, (cv, val) in self.meters.items():
            v = s.get(key)
            cv.delete("all")
            if v is None:
                val.config(text="–")
                continue
            color = ui.ERR if v >= 90 else ui.WARN if v >= 70 else ACCENT
            w = int(cv.winfo_width() * v / 100)
            cv.create_rectangle(0, 0, w, cv.winfo_height(), fill=color, width=0)
            val.config(text=f"{v:.0f}%")
        if self.res_win and self.res_win.winfo_exists():
            self.res_win.refresh()
        self.after(1000, self.update_meters)

    def open_resources(self):
        if self.res_win and self.res_win.winfo_exists():
            self.res_win.deiconify()
            self.res_win.lift()
            return
        self.res_win = ResourcesWindow(self)

    # ------------------------------------------------------------ finestre secondarie
    def dialog(self, title):
        px = self.t.px
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self)
        win.resizable(False, False)
        frame = ttk.Frame(win, style="Panel.TFrame", padding=px(20))
        frame.pack(fill="both", expand=True, padx=px(12), pady=px(12))
        win.after(10, lambda: ui.dark_titlebar(win))
        return win, frame

    def threads(self):
        """Core da usare dalla percentuale scelta (almeno 1; 100% = tutti)."""
        total = os.cpu_count() or 1
        pct = max(10, min(100, int(self.cpu_pct.get())))
        return 0 if pct >= 100 else max(1, round(total * pct / 100))

    def open_settings(self):
        px = self.t.px
        win, f = self.dialog("Impostazioni")
        ttk.Label(f, text="Impostazioni", style="Card.Head.TLabel").pack(anchor="w",
                                                                        pady=(0, px(10)))

        def path_row(label, var, browse):
            ttk.Label(f, text=label, style="Card.TLabel").pack(anchor="w", pady=(px(8), px(2)))
            row = ttk.Frame(f, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Entry(row, textvariable=var, width=46, style="Field.TEntry").pack(
                side="left", fill="x", expand=True)
            ttk.Button(row, text="…", width=3, style="Card.Small.TButton",
                       command=browse).pack(side="left", padx=(px(6), 0))

        proj = tk.StringVar(value=str(self.projects_dir))
        path_row("Cartella dove salvare i modelli", proj,
                 lambda: proj.set(filedialog.askdirectory(parent=win) or proj.get()))
        path_row("Bambu Studio (solo se non viene trovato da solo)", self.bambu,
                 lambda: self.bambu.set(filedialog.askopenfilename(parent=win)
                                        or self.bambu.get()))
        ttk.Separator(f).pack(fill="x", pady=px(14))
        ttk.Checkbutton(f, text="  Aggiorna il modello da solo quando cambio una rifinitura",
                        variable=self.auto, style="Card.Switch.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(f, text="  Suono quando il modello è pronto", variable=self.sound,
                        style="Card.Switch.TCheckbutton").pack(anchor="w", pady=(px(6), 0))
        ttk.Checkbutton(f, text="  Fotogrammi dal video scelti in automatico (consigliato)",
                        variable=self.fps_auto, style="Card.Switch.TCheckbutton").pack(
            anchor="w", pady=(px(6), 0))
        cpu_box = ttk.Frame(f, style="Card.TFrame")
        cpu_box.pack(fill="x", pady=(px(14), 0))
        ttk.Label(cpu_box, text="Potenza di calcolo", style="Card.TLabel").pack(side="left")
        cpu_lab = ttk.Label(cpu_box, style="Card.Muted.TLabel")
        cpu_lab.pack(side="right")
        cpu_scale = ttk.Scale(f, from_=10, to=100, variable=self.cpu_pct,
                              style="Card.Slim.Horizontal.TScale",
                              command=lambda v: self.cpu_pct.set(int(round(float(v) / 5) * 5)))
        cpu_scale.pack(fill="x")
        ui.Tooltip(cpu_scale, "Quanta CPU usare per creare i modelli. Meno = PC più usabile "
                              "nel frattempo ma elaborazione più lenta. Vale dal prossimo "
                              "avvio.", self.t)

        def cpu_text(*_):
            n = self.threads() or (os.cpu_count() or 1)
            cpu_lab.config(text=f"{self.cpu_pct.get()}%  ({n} di {os.cpu_count()} core)")
        self.cpu_pct.trace_add("write", cpu_text)
        cpu_text()
        fps_box = ttk.Frame(f, style="Card.TFrame")
        fps_lab = ttk.Label(fps_box, style="Card.Muted.TLabel")
        fps_lab.pack(side="right")
        ttk.Label(fps_box, text="Fotogrammi al secondo", style="Card.TLabel").pack(side="left")
        fps_scale = ttk.Scale(f, from_=0.5, to=10, variable=self.fps,
                              style="Card.Slim.Horizontal.TScale")

        def fps_state(*_):
            fps_lab.config(text=f"{self.fps.get():.1f}")
            if self.fps_auto.get():
                fps_box.pack_forget()
                fps_scale.pack_forget()
            else:
                fps_box.pack(fill="x", pady=(px(8), 0), before=bottom)
                fps_scale.pack(fill="x", before=bottom)
        bottom = ttk.Frame(f, style="Card.TFrame")
        bottom.pack(fill="x", pady=(px(18), 0))
        self.fps.trace_add("write", fps_state)
        self.fps_auto.trace_add("write", fps_state)
        fps_state()

        def close():
            new = Path(clean_path(proj.get()) or DATA / "progetti")
            if new != self.projects_dir:
                self.projects_dir = new
            self.save_cfg()
            win.destroy()
        ttk.Button(bottom, text="Chiudi", style="Card.Primary.TButton", command=close).pack(
            side="right")
        win.protocol("WM_DELETE_WINDOW", close)
        win.grab_set()

    def open_help(self):
        px = self.t.px
        win, f = self.dialog("Come si fa")
        box = ttk.Frame(f, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        text = tk.Text(box, bg=CARD, fg=FG, bd=0, wrap="word", width=72, height=30,
                       font=self.t.f["base"], highlightthickness=0, padx=px(4))
        scroll = ttk.Scrollbar(box, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("h", font=self.t.f["head"], spacing1=px(10), spacing3=px(4))
        text.tag_configure("m", foreground=MUTED)
        for kind, line in (
                ("h", "Come fare le foto (la cosa più importante)"),
                ("", "• Il soggetto sta fermo, chi riprende gli gira attorno: 2-3 giri lenti, "
                     "uno all'altezza degli occhi e uno un po' più in alto.\n"
                     "• 50-100 foto, oppure un video di 30-60 secondi senza scatti bruschi.\n"
                     "• Luce uniforme e niente controluce; ogni foto deve essere nitida.\n"
                     "• Il davanti lo decide il volto, se c'è; altrimenti la prima foto: "
                     "comincia dal davanti.\n"
                     "• I selfie non funzionano: si muove la testa e resta ferma la stanza.\n"
                     "• Oggetti lucidi, trasparenti o tutti di un colore sono difficili: in quel "
                     "caso il programma usa l'IA."),
                ("h", "Un busto di una persona, con un video"),
                ("", "Serve un'altra persona che riprende: chi viene ripreso deve restare "
                     "immobile.\n"
                     "• Chi posa: seduto su uno sgabello, schiena dritta, sguardo fisso su un "
                     "punto, espressione neutra e bocca chiusa. Non muoverti per tutto il "
                     "video, nemmeno con gli occhi.\n"
                     "• Luce: diffusa e uniforme, per esempio vicino a una finestra con il "
                     "cielo coperto o all'aperto all'ombra. Niente faretti né ombre nette sul "
                     "viso.\n"
                     "• Sfondo: una stanza normale con qualche oggetto va meglio di un muro "
                     "bianco liscio.\n"
                     "• Chi riprende: gira lentamente in cerchio a circa un metro, telefono "
                     "dritto in verticale e testa sempre al centro. Tre giri: all'altezza degli "
                     "occhi, poco più in alto guardando un po' in giù, poco più in basso "
                     "guardando un po' in su.\n"
                     "• Durata: 1-2 minuti, senza scatti. Meglio troppo lento che troppo "
                     "veloce: le immagini mosse vengono scartate.\n"
                     "• Telefono: 1080p o 4K, niente zoom, niente filtri, stabilizzazione "
                     "attiva."),
                ("m", "Capelli molto vaporosi o ricci escono un po' «a blocco». Gli occhiali "
                      "riflettono: meglio toglierli. Barba e vestiti scuri senza trama possono "
                      "venire meno definiti."),
                ("h", "I passaggi"),
                ("", "1. Foto: sceglie i fotogrammi più nitidi e li raddrizza.\n"
                     "2. Camere: COLMAP capisce da dove è stata scattata ogni foto.\n"
                     "3. Nuvola 3D: OpenMVS calcola milioni di punti della superficie.\n"
                     "4. Superficie: toglie lo sfondo, chiude il modello, fa la base piatta e "
                     "lo porta all'altezza scelta.\n"
                     "5. Pronto: lo apri in Bambu Studio (o lo scolpisci prima in Blender)."),
                ("h", "Il visualizzatore"),
                ("", "Trascina per girare il modello, rotella per lo zoom, tasto destro per "
                     "spostarlo, doppio clic per ricentrarlo. Tasti 1-5: davanti, destra, "
                     "dietro, sinistra, sopra. Barra spaziatrice: lo fa girare da solo. "
                     "Il piatto con la griglia da 1 cm è quello delle stampanti Bambu "
                     "(256 mm).\n"
                     "Tasto Q (o ⊞ nella barra a sinistra): quattro viste con le quote in mm. "
                     "Doppio clic su una vista per ingrandirla.\n"
                     "Tasto C (o ◐): il modello con i colori delle foto, se l'hai creato con "
                     "«Colori dalle foto» acceso. I colori restano anche dopo i ritocchi."),
                ("h", "Togliere i pezzi che non servono"),
                ("", "✂ nella barra a sinistra: trascina una linea sul modello, poi clicca la "
                     "parte da togliere (si colora di rosso). Se la linea è quasi orizzontale il "
                     "taglio diventa orizzontale esatto, così il nuovo fondo resta piatto. ↶ "
                     "annulla l'ultimo taglio, Esc rinuncia. I tagli restano anche cambiando "
                     "levigatura, altezza o orientamento."),
                ("h", "Ritoccare a mano"),
                ("", "✎ nella barra a sinistra (o tasto R) apre il pannello Ritocca. Leviga: "
                     "trascina sul modello per togliere grana e bozzi. Erodi: scava piano piano, "
                     "come carta vetrata. Base qui: clicca la zona che deve poggiare sul piatto, "
                     "il modello si gira e lì diventa piatto. Dimensione e Intensità regolano il "
                     "pennello; Ruota gira il modello sui tre assi del passo scelto. Trascinando "
                     "fuori dal modello giri la vista. Ctrl+Z annulla, «Torna all'originale» "
                     "toglie tutti i ritocchi. I ritocchi si salvano da soli e vanno in Bambu "
                     "Studio; se rifai il modello (levigatura, taglio, orientamento) si perdono."),
                ("h", "Scorciatoie"),
                ("", "Ctrl+O: scegli foto · Ctrl+Maiusc+O: scegli video · F5: crea il "
                     "modello · Esc: ferma"),
        ):
            text.insert("end", line + "\n", kind or ())
        text.config(state="disabled")
        ttk.Button(f, text="Chiudi", style="Card.Primary.TButton",
                   command=win.destroy).pack(anchor="e", pady=(px(12), 0))

    # ------------------------------------------------------------ chiusura
    def save_cfg(self):
        try:
            self.cfg.update({
                "versione": CFG_VERSION, "projects_dir": str(self.projects_dir),
                "legacy": self.legacy, "height": round(self.height_mm.get(), 1),
                "quality": self.quality.get(), "bambu": self.bambu.get(),
                "auto": self.auto.get(), "sound": self.sound.get(),
                "fps_auto": self.fps_auto.get(), "fps": round(self.fps.get(), 2),
                "cpu": int(self.cpu_pct.get()), "colori": self.want_colors.get()})
            self.cfg.pop("out", None)
            write_json(CFG, self.cfg)
        except tk.TclError:
            pass

    def on_close(self):
        self.monitor.stop()
        if self.running() and self.run_step == 1 and not messagebox.askyesno(
                "Esci", "Il modello non è finito: chiudendo si interrompe. Uscire?"):
            return
        self.stop()
        self.save_project()
        self.save_cfg()
        self.destroy()


class ResourcesWindow(tk.Toplevel):
    """Finestra con l'uso di CPU, memoria, GPU, memoria video e disco, con i grafici
    degli ultimi 90 secondi (come una piccola Gestione attività)."""

    ROWS = (("cpu", "Processore (CPU)"), ("ram", "Memoria (RAM)"), ("gpu", "Scheda video (GPU)"),
            ("vram", "Memoria video"))

    def __init__(self, app):
        super().__init__(app)
        self.app, self.t, self.m = app, app.t, app.monitor
        px = self.t.px
        self.title("Risorse del PC")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.after(10, lambda: ui.dark_titlebar(self))
        frame = ttk.Frame(self, style="Panel.TFrame", padding=px(18))
        frame.pack(fill="both", expand=True, padx=px(12), pady=px(12))
        self.rows = {}
        GW, GH = px(300), px(64)
        for key, title in self.ROWS:
            row = ttk.Frame(frame, style="Card.TFrame")
            row.pack(fill="x", pady=(0, px(12)))
            row.columnconfigure(0, minsize=px(150))
            left = ttk.Frame(row, style="Card.TFrame")
            left.grid(row=0, column=0, sticky="nw")
            ttk.Label(left, text=title, style="Card.Muted.TLabel").pack(anchor="w")
            big = tk.Label(left, text="–", bg=CARD, fg=FG, font=(self.t.ui_semi, 18))
            big.pack(anchor="w")
            det = ttk.Label(left, style="Card.Faint.TLabel")
            det.pack(anchor="w")
            cv = tk.Canvas(row, width=GW, height=GH, bg="#1c1e23", highlightthickness=0, bd=0)
            cv.grid(row=0, column=1, sticky="e", pady=px(4))
            row.columnconfigure(1, weight=1)
            self.rows[key] = (big, det, cv)
        ttk.Label(frame, text="Core del processore", style="Card.Muted.TLabel").pack(anchor="w")
        self.cores = tk.Canvas(frame, height=px(46), bg=CARD, highlightthickness=0, bd=0)
        self.cores.pack(fill="x", pady=(px(4), px(12)))
        disk = ttk.Frame(frame, style="Card.TFrame")
        disk.pack(fill="x")
        ttk.Label(disk, text="Spazio su disco (cartella dei modelli)",
                  style="Card.Muted.TLabel").pack(anchor="w")
        self.disk_bar = tk.Canvas(disk, height=px(8), bg=FIELD, highlightthickness=0, bd=0)
        self.disk_bar.pack(fill="x", pady=px(4))
        self.disk_lab = ttk.Label(disk, style="Card.Faint.TLabel")
        self.disk_lab.pack(anchor="w")
        names = " · ".join(x for x in (self.m.cpu_name, self.m.gpu_name) if x)
        ttk.Label(frame, text=(names + "\n" if names else "") +
                  "La ricostruzione usa il processore (COLMAP e OpenMVS su CPU, anche con "
                  "schede AMD); la scheda video serve al visualizzatore 3D.",
                  style="Card.Faint.TLabel", wraplength=px(440), justify="left").pack(
            anchor="w", pady=(px(14), 0))
        self.refresh()

    def refresh(self):
        s, px = self.m.last, self.t.px
        for key, (big, det, cv) in self.rows.items():
            v = s.get(key)
            big.config(text="–" if v is None else f"{v:.0f}%")
            if key == "cpu":
                det.config(text=f"{self.m.n_threads} thread")
            elif key == "ram" and "ram_total" in s:
                det.config(text=f"{gb(s['ram_used'])} di {gb(s['ram_total'])} GB")
            elif key == "vram" and "vram_used" in s:
                tot = f" di {gb(s['vram_total'])}" if s.get("vram_total") else ""
                det.config(text=f"{gb(s['vram_used'])}{tot} GB")
            elif key == "gpu":
                det.config(text="non disponibile" if v is None else "")
            self.graph(cv, list(self.m.history[key]))
        cv = self.cores
        cv.delete("all")
        cores = self.m.cores
        if cores:
            w = cv.winfo_width() or px(440)
            h = cv.winfo_height() or px(46)
            gap = px(4)
            bw = (w - gap * (len(cores) - 1)) / len(cores)
            for i, v in enumerate(cores):
                x0 = i * (bw + gap)
                cv.create_rectangle(x0, 0, x0 + bw, h, fill="#1c1e23", width=0)
                color = ui.ERR if v >= 90 else ui.WARN if v >= 70 else ACCENT
                cv.create_rectangle(x0, h - h * v / 100, x0 + bw, h, fill=color, width=0)
        if "disk_total" in s:
            used = 1 - s["disk_free"] / max(s["disk_total"], 1)
            self.disk_bar.delete("all")
            w = self.disk_bar.winfo_width() or px(440)
            color = ui.ERR if used > 0.95 else ui.WARN if used > 0.85 else ACCENT
            self.disk_bar.create_rectangle(0, 0, w * used, px(8), fill=color, width=0)
            self.disk_lab.config(text=f"Liberi {gb(s['disk_free'])} GB su "
                                      f"{gb(s['disk_total'])} GB")

    def graph(self, cv, values):
        cv.delete("all")
        w, h = int(cv["width"]), int(cv["height"])
        for frac in (0.25, 0.5, 0.75):
            cv.create_line(0, h * frac, w, h * frac, fill="#262930")
        pts = [(i * w / (len(values) - 1), h - 2 - (h - 4) * v / 100)
               for i, v in enumerate(values) if v is not None]
        if len(pts) >= 2:
            poly = [(pts[0][0], h)] + pts + [(pts[-1][0], h)]
            cv.create_polygon(*[c for p in poly for c in p], fill="#25365e", outline="")
            cv.create_line(*[c for p in pts for c in p], fill=ACCENT, width=2)


def single_instance():
    """True se è l'unica finestra aperta: due finestre potrebbero elaborare lo stesso
    progetto insieme e rovinarlo (oltre a dimezzare la velocità)."""
    import socket
    global _LOCK
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", 47613))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    ui.app_id()
    if not single_instance():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} è già aperto: usa la finestra già aperta "
                                        "(controlla la barra delle applicazioni).")
        sys.exit()
    App().mainloop()
