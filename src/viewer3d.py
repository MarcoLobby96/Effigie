"""Visualizzatore 3D dell'interfaccia: il modello si gira con il mouse.

Usa OpenGL 1.1 della scheda video tramite ctypes (nessuna libreria in più, solo Windows);
se non è disponibile mostra le quattro viste dell'anteprima statica (anteprima.png).
Comandi: trascina = ruota, rotella = zoom, tasto destro/centrale = sposta,
doppio clic = vista iniziale.
"""
import ctypes
import math
import re
import sys
import threading
import tkinter as tk
from pathlib import Path

import numpy as np

PLATE_MM = 256                       # piatto delle stampanti Bambu Lab (X1, P1, A1)
TOP, BOTTOM = (0.20, 0.21, 0.25), (0.075, 0.08, 0.095)   # sfondo sfumato
MODEL_RGB = (0.84, 0.82, 0.78)       # "PLA bianco" un po' caldo
# vista: (azimut, elevazione) della camera; destra = camera dal lato +X, come negli slicer
VIEWS = {"front": (0, 12), "right": (90, 12), "back": (180, 12), "left": (270, 12),
         "top": (0, 89)}
STATIC_INDEX = {"front": 0, "left": 1, "back": 2, "right": 3}   # quarti di anteprima.png


# ----------------------------------------------------------------- mesh
def load_stl(path):
    """Vertici (N×3) e facce (M×3) di un STL binario o di testo, con i vertici uniti."""
    data = Path(path).read_bytes()
    n = int.from_bytes(data[80:84], "little") if len(data) >= 84 else 0
    if n and len(data) >= 84 + 50 * n:
        rec = np.frombuffer(data, np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)),
                                            ("a", "<u2")]), count=n, offset=84)
        tri = np.ascontiguousarray(rec["v"].reshape(-1, 3))
    else:
        nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data.decode("latin-1"))
        tri = np.array(nums, dtype=np.float32).reshape(-1, 3)
    if not len(tri):
        raise ValueError("STL vuoto")
    keys = tri.view(np.dtype((np.void, 12))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    return tri[first], inverse.reshape(-1, 3).astype(np.uint32)


def vertex_normals(V, F):
    v0, v1, v2 = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)                    # pesate con l'area
    N = np.empty_like(V)
    idx = F.ravel()
    for c in range(3):
        N[:, c] = np.bincount(idx, weights=np.repeat(fn[:, c], 3), minlength=len(V))
    N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-12
    return N.astype(np.float32)


def vertex_colors(work, V):
    """Colori delle foto per i vertici del modello mostrato: colori.npz (punti nelle
    coordinate della ricostruzione) riportati con la trasformazione attuale, poi il colore
    del punto più vicino. None se il modello non ha colori."""
    import json
    work = Path(work)
    try:
        data = np.load(work / "colori.npz")
        T = np.array(json.loads((work / "busto.json").read_text())["transform"], float)
    except (OSError, KeyError, ValueError):
        return None
    pts = (np.c_[data["points"], np.ones(len(data["points"]))] @ T.T)[:, :3]
    from scipy.spatial import cKDTree
    idx = cKDTree(pts).query(V, workers=-1)[1]
    return np.ascontiguousarray(data["colors"][idx], np.uint8)


def has_colors(work):
    return (Path(work) / "colori.npz").is_file()


def mesh_info(V, F):
    """Dimensioni (mm), volume (mm³) e area (mm²) di una mesh chiusa."""
    v0, v1, v2 = (V[F[:, k]].astype(np.float64) for k in range(3))
    cross = np.cross(v1 - v0, v2 - v0)
    return {"extents": (V.max(0) - V.min(0)).tolist(), "faces": int(len(F)),
            "volume": float(abs(np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum()) / 6),
            "area": float(np.linalg.norm(cross, axis=1).sum() / 2)}


def model_file(work):
    """Il modello da mostrare: quello scolpito in Blender se più recente."""
    work = Path(work)
    stl, sculpt = work / "busto.stl", work / "scolpito.stl"
    if sculpt.is_file() and (not stl.is_file() or sculpt.stat().st_mtime > stl.stat().st_mtime):
        return sculpt
    return stl if stl.is_file() else None


# ----------------------------------------------------------------- matrici
def perspective(fovy, aspect, near, far):
    f = 1 / math.tan(math.radians(fovy) / 2)
    return np.array([[f / aspect, 0, 0, 0], [0, f, 0, 0],
                     [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                     [0, 0, -1, 0]], np.float32)


def orthographic(half_w, half_h, depth):
    m = np.eye(4)
    m[0, 0], m[1, 1], m[2, 2] = 1 / half_w, 1 / half_h, -1 / depth
    return m


# viste ortografiche della griglia: (nome, direzione da cui si guarda, "su" dello schermo,
# assi del modello mostrati in orizzontale e in verticale)
ORTHO = (("Davanti", (0, -1, 0), (0, 0, 1), 0, 2),
         ("Destra", (1, 0, 0), (0, 0, 1), 1, 2),
         ("Alto", (0, 0, 1), (0, 1, 0), 0, 1))
QUAD_VIEW = {"Davanti": "front", "Destra": "right", "Alto": "top", "3D": None}


def look_at(eye, target, up=(0, 0, 1)):
    f = np.asarray(target, float) - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    if np.linalg.norm(s) < 1e-6:                      # guardando dritto in basso
        s = np.cross(f, (0, 1, 0))
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    M = np.eye(4, dtype=np.float32)
    M[0, :3], M[1, :3], M[2, :3] = s, u, -f
    M[:3, 3] = -M[:3, :3] @ eye
    return M


# ----------------------------------------------------------------- OpenGL (Windows)
class PFD(ctypes.Structure):
    _fields_ = [("nSize", ctypes.c_ushort), ("nVersion", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("iPixelType", ctypes.c_ubyte),
                ("cColorBits", ctypes.c_ubyte),
                *[(f"color{i}", ctypes.c_ubyte) for i in range(10)],   # bit/shift dei colori
                ("cAccumBits", ctypes.c_ubyte), *[(f"accum{i}", ctypes.c_ubyte) for i in range(4)],
                ("cDepthBits", ctypes.c_ubyte), ("cStencilBits", ctypes.c_ubyte),
                ("cAuxBuffers", ctypes.c_ubyte), ("iLayerType", ctypes.c_ubyte),
                ("bReserved", ctypes.c_ubyte), ("dwLayerMask", ctypes.c_ulong),
                ("dwVisibleMask", ctypes.c_ulong), ("dwDamageMask", ctypes.c_ulong)]


GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT = 0x4000, 0x0100
GL_DEPTH_TEST, GL_LIGHTING, GL_LIGHT0, GL_LIGHT1 = 0x0B71, 0x0B50, 0x4000, 0x4001
GL_DIFFUSE, GL_SPECULAR, GL_POSITION = 0x1201, 0x1202, 0x1203
GL_FRONT_AND_BACK, GL_SHININESS, GL_AMBIENT_AND_DIFFUSE = 0x0408, 0x1601, 0x1602
GL_LIGHT_MODEL_AMBIENT, GL_LIGHT_MODEL_TWO_SIDE, GL_NORMALIZE = 0x0B53, 0x0B52, 0x0BA1
GL_SMOOTH, GL_PROJECTION, GL_MODELVIEW = 0x1D01, 0x1701, 0x1700
GL_VERTEX_ARRAY, GL_NORMAL_ARRAY, GL_FLOAT, GL_UNSIGNED_INT = 0x8074, 0x8075, 0x1406, 0x1405
GL_TRIANGLES, GL_LINES, GL_QUADS, GL_TRIANGLE_FAN = 0x0004, 0x0001, 0x0007, 0x0006
GL_COMPILE, GL_BLEND, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA = 0x1300, 0x0BE2, 0x0302, 0x0303
GL_MULTISAMPLE, GL_RENDERER, GL_RGBA, GL_UNSIGNED_BYTE = 0x809D, 0x1F01, 0x1908, 0x1401
GL_PACK_ALIGNMENT, GL_LINE_LOOP = 0x0D05, 0x0002
GL_COLOR_ARRAY, GL_COLOR_MATERIAL = 0x8076, 0x0B57


class GL:
    """Le funzioni di OpenGL 1.1 usate qui (opengl32.dll le esporta direttamente)."""
    SIGS = {
        "glClearColor": (None, "ffff"), "glClear": (None, "u"), "glViewport": (None, "iiii"),
        "glMatrixMode": (None, "u"), "glLoadIdentity": (None, ""), "glLoadMatrixf": (None, "p"),
        "glEnable": (None, "u"), "glDisable": (None, "u"), "glLightfv": (None, "uup"),
        "glLightModelfv": (None, "up"), "glLightModeli": (None, "ui"),
        "glMaterialfv": (None, "uup"), "glMaterialf": (None, "uuf"), "glShadeModel": (None, "u"),
        "glEnableClientState": (None, "u"), "glDisableClientState": (None, "u"),
        "glVertexPointer": (None, "iuip"), "glNormalPointer": (None, "uip"),
        "glDrawElements": (None, "uiup"), "glGenLists": ("u", "i"), "glNewList": (None, "uu"),
        "glEndList": (None, ""), "glCallList": (None, "u"), "glDeleteLists": (None, "ui"),
        "glBegin": (None, "u"), "glEnd": (None, ""), "glVertex3f": (None, "fff"),
        "glColor4f": (None, "ffff"), "glLineWidth": (None, "f"), "glBlendFunc": (None, "uu"),
        "glDepthMask": (None, "b"), "glColorPointer": (None, "iuip"),
        "glColorMaterial": (None, "uu"), "glGetString": ("s", "u"),
        "glReadPixels": (None, "iiiiuup"), "glPixelStorei": (None, "ui"), "glFinish": (None, ""),
    }
    TYPES = {"f": ctypes.c_float, "u": ctypes.c_uint, "i": ctypes.c_int, "p": ctypes.c_void_p,
             "b": ctypes.c_ubyte, "s": ctypes.c_char_p, None: None}

    def __init__(self):
        lib = ctypes.windll.opengl32
        for name, (res, args) in self.SIGS.items():
            fn = getattr(lib, name)
            fn.restype = self.TYPES[res]
            fn.argtypes = [self.TYPES[a] for a in args]
            setattr(self, name[2:], fn)


def floats(*v):
    return (ctypes.c_float * len(v))(*v)


def _wgl():
    u32, g32, ogl = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.opengl32
    vp = ctypes.c_void_p
    u32.GetDC.restype, u32.GetDC.argtypes = vp, [vp]
    u32.ReleaseDC.argtypes = [vp, vp]
    u32.CreateWindowExW.restype = vp
    u32.CreateWindowExW.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                    ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, vp, vp, vp, vp]
    u32.DestroyWindow.argtypes = [vp]
    g32.ChoosePixelFormat.argtypes = [vp, ctypes.POINTER(PFD)]
    g32.SetPixelFormat.argtypes = [vp, ctypes.c_int, ctypes.POINTER(PFD)]
    g32.DescribePixelFormat.argtypes = [vp, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(PFD)]
    g32.SwapBuffers.argtypes = [vp]
    ogl.wglCreateContext.restype, ogl.wglCreateContext.argtypes = vp, [vp]
    ogl.wglMakeCurrent.argtypes = [vp, vp]
    ogl.wglDeleteContext.argtypes = [vp]
    ogl.wglGetProcAddress.restype, ogl.wglGetProcAddress.argtypes = vp, [ctypes.c_char_p]
    return u32, g32, ogl


def basic_pfd():
    pfd = PFD()
    pfd.nSize, pfd.nVersion = ctypes.sizeof(PFD), 1
    pfd.dwFlags = 0x4 | 0x20 | 0x1                 # finestra, OpenGL, doppio buffer
    pfd.cColorBits, pfd.cDepthBits = 24, 24
    return pfd


def multisample_format(samples=8):
    """Formato con antialiasing (bordi lisci): per chiederlo a OpenGL (estensione
    WGL_ARB_pixel_format) serve un contesto provvisorio su una finestra provvisoria."""
    u32, g32, ogl = _wgl()
    hwnd = u32.CreateWindowExW(0, "STATIC", "gl", 0, 0, 0, 16, 16, None, None, None, None)
    if not hwnd:
        return None
    hdc = u32.GetDC(hwnd)
    ctx = None
    try:
        pfd = basic_pfd()
        g32.SetPixelFormat(hdc, g32.ChoosePixelFormat(hdc, ctypes.byref(pfd)), ctypes.byref(pfd))
        ctx = ogl.wglCreateContext(hdc)
        if not ctx or not ogl.wglMakeCurrent(hdc, ctx):
            return None
        addr = ogl.wglGetProcAddress(b"wglChoosePixelFormatARB")
        if not addr:
            return None
        choose = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_float), ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_int),
                                    ctypes.POINTER(ctypes.c_uint))(addr)
        for s in (samples, 4):
            attrs = (ctypes.c_int * 19)(0x2001, 1, 0x2010, 1, 0x2011, 1, 0x2013, 0x202B,
                                        0x2014, 24, 0x2022, 24, 0x2041, 1, 0x2042, s, 0, 0, 0)
            fmt, count = ctypes.c_int(), ctypes.c_uint()
            if choose(hdc, attrs, None, 1, ctypes.byref(fmt), ctypes.byref(count)) and count.value:
                return fmt.value
        return None
    finally:
        ogl.wglMakeCurrent(None, None)
        if ctx:
            ogl.wglDeleteContext(ctx)
        u32.ReleaseDC(hwnd, hdc)
        u32.DestroyWindow(hwnd)


# ----------------------------------------------------------------- widget
class Viewer(tk.Frame):
    """Riquadro con il modello 3D. load(cartella) mostra busto.stl (o scolpito.stl)."""

    def __init__(self, master, bg="#16171b", fg="#9aa0aa", font=("Segoe UI", 11), panel=None,
                 radius=0, placeholder_style=None, **kw):
        """bg = colore dietro il riquadro (angoli), panel = colore del riquadro vuoto,
        radius = angoli arrotondati in pixel, placeholder_style = stile ttk del riquadro
        che sostituisce il modello quando c'è solo un messaggio."""
        super().__init__(master, bg=bg, **kw)
        self.bg = panel or bg
        self.radius = radius
        self.mesh = None            # (V, F, N)
        self.bounds = None
        self.az, self.el, self.dist, self.target = 0.0, 12.0, 1.0, np.zeros(3)
        self.spin = False
        self.on_load = None         # richiamata con le info del modello caricato
        self.quad = False           # griglia 2×2: tre viste quotate + vista 3D
        self.quad_labels = []
        self.show_colors = False    # modello con i colori delle foto (se ci sono)
        self.colors = None
        self.on_cut = None          # richiamata con (normale, punto) del piano di taglio, in mm
        self.cut_mode = False
        self._cut = None            # [x0, y0, x1, y1] in pixel, poi il lato da togliere
        self._cut_side = None
        self.sculptor = None        # ritocchi a mano (sculpt.Sculptor) mentre c'è uno strumento
        self.tool = None            # "smooth", "erode", "base"
        self.brush_mm, self.brush_strength = 6.0, 0.5
        self.on_sculpt = None       # richiamata dopo ogni modifica a mano (per salvarla)
        self.on_tool_end = None     # richiamata quando lo strumento si chiude da solo
        self._brush_at = None       # (punto, normale) sotto il mouse
        self.renderer = ""
        self._pending = False
        self._drag = None
        self._list = None
        self._loading = 0
        self.gl = None
        self.mode = "gl" if sys.platform == "win32" else "static"
        # superficie OpenGL senza sfondo: così Tk non ci disegna sopra
        self.surface = tk.Frame(self, bg="")
        self.static = tk.Label(self, bg=self.bg, bd=0)
        if placeholder_style:
            from tkinter import ttk
            self.placeholder = ttk.Frame(self, style=placeholder_style)
        else:
            self.placeholder = tk.Frame(self, bg=self.bg)
        self.icon = tk.Label(self.placeholder, bg=self.bg, bd=0)
        self.msg = tk.Label(self.placeholder, bg=self.bg, fg=fg, font=font, justify="center")
        self.msg.place(relx=0.5, rely=0.5, anchor="center")
        self.static_views = []
        self.static_idx = 0
        if self.mode == "gl":
            self.surface.place(relx=0, rely=0, relwidth=1, relheight=1)
        else:
            self.static.place(relx=0, rely=0, relwidth=1, relheight=1)
        for w in (self.surface, self.static):
            w.bind("<ButtonPress-1>", lambda e: self._press(e, "rotate"))
            w.bind("<ButtonPress-2>", lambda e: self._press(e, "pan"))
            w.bind("<ButtonPress-3>", lambda e: self._press(e, "pan"))
            for b in (1, 2, 3):
                w.bind(f"<B{b}-Motion>", self._motion)
            w.bind("<ButtonRelease>", self._release)
            w.bind("<Motion>", self._hover)
            w.bind("<MouseWheel>", lambda e: self.zoom(0.88 if e.delta > 0 else 1 / 0.88))
            w.bind("<Button-4>", lambda e: self.zoom(0.88))
            w.bind("<Button-5>", lambda e: self.zoom(1 / 0.88))
            w.bind("<Double-Button-1>", self._double_click)
        self.surface.bind("<Configure>", self._resized)
        self.surface.bind("<Expose>", lambda e: self.redraw())
        self.static.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Destroy>", self._destroy, add="+")
        self.message("Qui comparirà il modello 3D.")
        # loader: cerchio che gira sopra il modello mentre si carica
        self.loader = tk.Canvas(self, width=120, height=96, bg=self.bg, highlightthickness=0, bd=0)
        self.loader.create_oval(40, 12, 80, 52, outline="#3a3d45", width=5)
        self._arc = self.loader.create_arc(40, 12, 80, 52, start=90, extent=100, style="arc",
                                           outline="#5b8def", width=5)
        self._loader_text = self.loader.create_text(60, 76, text="Carico…", fill=fg, font=font)
        self._loader_job = None
        self._loader_angle = 90
        self._loader_label, self._loader_pct, self._spin_pct = "Carico", None, None
        self._set_cursor = False

    # ------------------------------------------------------------ stato
    def message(self, text, icon=None):
        """Riquadro con un testo al posto del modello (None = torna il modello)."""
        if text:
            self.msg.config(text=text)
            if icon is not None:
                self.icon.config(image=icon)
                self.icon.image = icon
            self.placeholder.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.placeholder.lift()
            if getattr(self, "loader", None) is not None and self.loader.winfo_ismapped():
                tk.Misc.lift(self.loader)      # con il loader visibile il testo resta nascosto
            else:
                self._show_text()
        else:
            self.placeholder.place_forget()

    def _show_text(self):
        self.msg.place(relx=0.5, rely=0.5, anchor="center")
        if self.icon.cget("image"):
            self.icon.place(relx=0.5, rely=0.5, anchor="s", y=-self.msg.winfo_reqheight() // 2 - 12)
        else:
            self.icon.place_forget()

    def _hide_text(self):
        self.msg.place_forget()
        self.icon.place_forget()

    def show_loader(self, text="Carico il modello", pct=None):
        """Cerchio con percentuale: pieno fino a pct (0-100), oppure che gira se pct è None.
        Anche il puntatore del mouse diventa quello di attesa."""
        self._loader_label, self._loader_pct = text, pct
        w = 240                                 # larghezza fissa: niente avanzi di testo
        self.loader.config(width=w)
        self.loader.coords(self.loader.find_all()[0], w / 2 - 20, 12, w / 2 + 20, 52)
        self.loader.coords(self._arc, w / 2 - 20, 12, w / 2 + 20, 52)
        self.loader.coords(self._loader_text, w / 2, 76)
        self._loader_paint()
        self._hide_text()                       # mai testo e loader sovrapposti
        if not self.loader.winfo_ismapped():
            self.loader.place(relx=0.5, rely=0.5, anchor="center")
            tk.Misc.lift(self.loader)          # Canvas.lift alza un disegno, non il widget
        top = self.winfo_toplevel()
        if not top.cget("cursor"):
            top.config(cursor="watch")
            self._set_cursor = True
        if pct is None and self._loader_job is None:
            self._spin_loader()
        elif pct is not None and self._loader_job is not None:
            self.after_cancel(self._loader_job)
            self._loader_job = None

    def set_loader_pct(self, pct):
        """Aggiorna la percentuale (anche da un thread: viene letta dall'animazione)."""
        self._loader_pct = pct

    def _loader_paint(self):
        pct = self._loader_pct
        if pct is None:
            self.loader.itemconfigure(self._loader_text, text=f"{self._loader_label}…")
            return
        pct = max(0.0, min(100.0, float(pct)))
        self.loader.itemconfigure(self._arc, start=90, extent=-max(3.6 * pct, 1))
        self.loader.itemconfigure(self._loader_text, text=f"{self._loader_label}  {pct:.0f}%")

    def hide_loader(self):
        if self._loader_job is not None:
            self.after_cancel(self._loader_job)
            self._loader_job = None
        self.loader.place_forget()
        if self.placeholder.winfo_ismapped():
            self._show_text()
        if getattr(self, "_set_cursor", False):
            self.winfo_toplevel().config(cursor="")
            self._set_cursor = False

    def _spin_loader(self):
        """Animazione: gira; se un thread ha fissato una percentuale la mostra nel testo."""
        self._loader_angle = (self._loader_angle - 12) % 360
        self.loader.itemconfigure(self._arc, start=self._loader_angle, extent=100)
        label = f"{self._loader_label}…"
        if self._spin_pct is not None:
            label = f"{self._loader_label}  {self._spin_pct:.0f}%"
        self.loader.itemconfigure(self._loader_text, text=label)
        self._loader_job = self.after(30, self._spin_loader)

    def _resized(self, e):
        """Angoli arrotondati della superficie OpenGL (regione della finestra)."""
        if self.radius and sys.platform == "win32":
            try:
                g32, u32 = ctypes.windll.gdi32, ctypes.windll.user32
                g32.CreateRoundRectRgn.restype = ctypes.c_void_p
                u32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
                r = 2 * self.radius
                rgn = g32.CreateRoundRectRgn(0, 0, e.width + 1, e.height + 1, r, r)
                u32.SetWindowRgn(ctypes.c_void_p(self.surface.winfo_id()), ctypes.c_void_p(rgn), 1)
            except (OSError, AttributeError):
                pass
        self.redraw()

    def _end_tool(self):
        """Un altro modello (o nessuno): lo strumento di ritocco si chiude."""
        if self.tool or self.sculptor is not None:
            self.tool, self.sculptor, self._brush_at = None, None, None
            self.surface.config(cursor="")
            if self.on_tool_end:
                self.on_tool_end()

    def clear(self, text="Qui comparirà il modello 3D."):
        self._end_tool()
        self.mesh = None
        self.bounds = None
        self._loading += 1                  # annulla eventuali caricamenti in corso
        self.hide_loader()
        self.static_views = []
        self.static.config(image="")
        self.message(text)
        self.redraw()

    def load(self, work, keep_view=False):
        """Carica il modello della cartella (in un thread: un STL grande richiede ~1 s)."""
        path = model_file(work)
        if not path:
            return self.clear()
        self._end_tool()
        self._loading += 1
        token = self._loading
        preview = Path(work) / "anteprima.png"

        def job():
            try:
                self._spin_pct = 5
                V, F = load_stl(path)
                self._spin_pct = 55
                N = vertex_normals(V, F)
                self._spin_pct = 85
                self._pending_colors = vertex_colors(work, V) if self.show_colors else None
                result = (V, F, N, mesh_info(V, F))
                self._spin_pct = 100
            except (OSError, ValueError, MemoryError) as e:
                result = e
            try:
                self.after(0, lambda: self._loaded(token, result, path, preview, keep_view))
            except (RuntimeError, tk.TclError):            # finestra chiusa nel frattempo
                pass
        if self.mesh is None:
            self.message(" ")
        self._spin_pct = 0
        self.show_loader("Carico il modello")
        threading.Thread(target=job, daemon=True).start()

    def _loaded(self, token, result, path, preview, keep_view):
        if token != self._loading:
            return
        self.hide_loader()
        if isinstance(result, Exception):
            return self.clear(f"Modello non leggibile:\n{result}")
        V, F, N, info = result
        self.colors = getattr(self, "_pending_colors", None)
        self.mesh = (np.ascontiguousarray(V, np.float32), np.ascontiguousarray(F, np.uint32),
                     np.ascontiguousarray(N, np.float32))
        old = self.bounds
        self.bounds = (V.min(0), V.max(0))
        self.message(None)
        if self.mode == "gl" and self._ensure_gl():
            self._upload()
        else:
            self._go_static()
            self._load_static(preview)
        if keep_view and old is not None:
            self.redraw()
        else:
            self.reset()
        if self.on_load:
            info["path"] = path
            self.on_load(info)

    # ------------------------------------------------------------ camera
    def reset(self):
        if self.bounds is None:
            return
        lo, hi = self.bounds
        self.target = (lo + hi) / 2
        radius = float(np.linalg.norm(hi - lo)) / 2
        self.dist = radius / math.sin(math.radians(15)) * 1.05
        self.az, self.el = VIEWS["front"]
        self.static_idx = 0
        self.redraw()

    def set_colors(self, on, work):
        """Modello grigio o con i colori delle foto."""
        self.show_colors = bool(on)
        if work and model_file(work):
            self.load(work, keep_view=True)

    def set_quad(self, on):
        """Griglia 2×2 (Davanti, Destra, Alto con le quote, più la vista 3D) o vista singola."""
        self.quad = bool(on) and self.mode == "gl"
        if not self.quad:
            self._clear_quad_labels()
        self.redraw()

    def _double_click(self, e):
        """In griglia: il riquadro cliccato diventa la vista a tutto schermo."""
        if not self.quad:
            return self.reset()
        w, h = self.surface.winfo_width(), self.surface.winfo_height()
        col, row = int(e.x >= w / 2), int(e.y >= h / 2)
        name = (("Davanti", "Destra"), ("Alto", "3D"))[row][col]
        self.set_quad(False)
        if QUAD_VIEW[name]:
            self.view(QUAD_VIEW[name])

    def view(self, name):
        if self.bounds is None:
            return
        self.az, self.el = VIEWS[name]
        self.static_idx = STATIC_INDEX.get(name, 0)
        self.redraw()

    def zoom(self, factor):
        self.dist = float(np.clip(self.dist * factor, 1.0, 5000.0))
        self.redraw()

    def toggle_spin(self):
        self.spin = not self.spin
        if self.spin:
            self._spin_step()
        return self.spin

    def _spin_step(self):
        if not self.spin or self.mesh is None:
            self.spin = False
            return
        if self.mode == "gl":
            self.az = (self.az - 1.0) % 360
        else:
            self.static_idx = (self.static_idx + 1) % 4
        self.redraw()
        self.after(30 if self.mode == "gl" else 900, self._spin_step)

    # ------------------------------------------------------------ taglio
    def start_cut(self):
        """Taglio: si trascina una linea sul modello, poi si clicca la parte da togliere."""
        if self.mesh is None or self.mode != "gl":
            return False
        if self.quad:
            self.set_quad(False)
        self.cut_mode, self._cut, self._cut_side = True, None, None
        self.surface.config(cursor="crosshair")
        self.redraw()
        return True

    def cancel_cut(self):
        self.cut_mode, self._cut, self._cut_side = False, None, None
        self.surface.config(cursor="")
        self.redraw()

    def _ray(self, x, y):
        """Punto vicino e lontano (mm) sotto il pixel (x, y) della vista prospettica."""
        w, h = max(self.surface.winfo_width(), 1), max(self.surface.winfo_height(), 1)
        proj, view = self._matrices(w, h)
        m = np.linalg.inv((proj.T.astype(float)) @ (view.T.astype(float)))
        nx, ny = 2 * x / w - 1, 1 - 2 * y / h
        a, b = m @ [nx, ny, -1, 1], m @ [nx, ny, 1, 1]
        return a[:3] / a[3], b[:3] / b[3]

    def _cut_plane(self, side_x, side_y):
        """Piano che passa per la linea tracciata (e per chi guarda) e lato da togliere."""
        x0, y0, x1, y1 = self._cut
        a0, b0 = self._ray(x0, y0)
        a1, b1 = self._ray(x1, y1)
        n = np.cross(b0 - a0, b1 - a0)
        if np.linalg.norm(n) < 1e-9:
            return None
        n /= np.linalg.norm(n)
        p = a0
        # quasi orizzontale (la vista è un po' dall'alto): taglio orizzontale esatto, così il
        # nuovo fondo è piatto; quasi verticale: taglio verticale esatto
        lo, hi = (np.asarray(b, float) for b in self.bounds)
        c = (lo + hi) / 2
        if abs(n[2]) > math.cos(math.radians(25)):
            z = p[2] - (n[0] * (c[0] - p[0]) + n[1] * (c[1] - p[1])) / n[2]
            n, p = np.array([0.0, 0.0, math.copysign(1.0, n[2])]), np.array([c[0], c[1], z])
        elif abs(n[2]) < math.sin(math.radians(25)):
            n = np.array([n[0], n[1], 0.0]) / max(np.linalg.norm(n[:2]), 1e-9)
        _, bs = self._ray(side_x, side_y)
        a1s, _ = self._ray(side_x, side_y)
        # lato cliccato: il punto sotto il mouse, alla profondità del modello
        t = np.clip((c - a1s) @ (bs - a1s) / max((bs - a1s) @ (bs - a1s), 1e-9), 0, 1)
        q = a1s + (bs - a1s) * t
        if (q - p) @ n > 0:                     # il lato cliccato va tolto: si tiene l'altro
            n = -n
        return n, p

    def _side(self, x, y):
        x0, y0, x1, y1 = self._cut
        return 1 if (x1 - x0) * (y - y0) - (y1 - y0) * (x - x0) > 0 else -1

    def _hover(self, e):
        if self.tool:
            self._brush_at = self._pick(e.x, e.y)
            self.redraw()
            return
        if self.cut_mode and self._cut and len(self._cut) == 4 and self._drag is None:
            side = self._side(e.x, e.y)
            if side != self._cut_side:
                self._cut_side = side
                self.redraw()

    def _release(self, e):
        if self._drag and self._drag[0] == "brush":
            self._drag = None
            self._sculpted()
            return
        if self.cut_mode and self._drag and self._drag[0] == "cut":
            self._drag = None
            x0, y0 = self._cut[:2]
            if math.hypot(e.x - x0, e.y - y0) < 8:          # solo un clic: ricomincia
                self._cut = None
            self.redraw()
            return
        self._drag = None

    def _draw_cut_overlay(self, w, h):
        gl = self.gl
        if not self._cut or len(self._cut) < 4:
            return
        x0, y0, x1, y1 = self._cut
        p0 = np.array([2 * x0 / w - 1, 1 - 2 * y0 / h])
        p1 = np.array([2 * x1 / w - 1, 1 - 2 * y1 / h])
        d = p1 - p0
        if np.linalg.norm(d) < 1e-6:
            return
        d /= np.linalg.norm(d)
        a, b = p0 - d * 10, p1 + d * 10
        gl.Disable(GL_DEPTH_TEST)
        gl.Disable(GL_LIGHTING)
        gl.Enable(GL_BLEND)
        gl.BlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        gl.MatrixMode(GL_PROJECTION)
        gl.LoadIdentity()
        gl.MatrixMode(GL_MODELVIEW)
        gl.LoadIdentity()
        if self._cut_side and self._drag is None:
            # perpendicolare dalla parte del mouse (in pixel y cresce verso il basso)
            perp = np.array([-d[1], d[0]]) * (-self._cut_side)
            gl.Color4f(0.9, 0.25, 0.22, 0.28)
            gl.Begin(GL_QUADS)
            for q in (a, b, b + perp * 10, a + perp * 10):
                gl.Vertex3f(q[0], q[1], 0)
            gl.End()
        gl.LineWidth(2.0)
        gl.Color4f(1.0, 0.42, 0.36, 1)
        gl.Begin(GL_LINES)
        gl.Vertex3f(a[0], a[1], 0)
        gl.Vertex3f(b[0], b[1], 0)
        gl.End()
        gl.LineWidth(1.0)

    def _press(self, e, kind):
        e.widget.focus_set()
        if self.cut_mode and kind == "rotate":
            if self._cut and len(self._cut) == 4:       # secondo clic: il lato da togliere
                plane = self._cut_plane(e.x, e.y)
                self.cancel_cut()
                if plane and self.on_cut:
                    self.on_cut(*plane)
                return
            self._cut = [e.x, e.y, e.x, e.y]
            self._drag = ("cut", e.x, e.y)
            return
        if self.tool and kind == "rotate":
            hit = self._pick(e.x, e.y)
            if hit is not None:                     # sul modello: pennello; fuori: gira la vista
                if self.tool == "base":
                    if self.sculptor.base_at(hit[0], self.brush_mm):
                        self._refresh_mesh(recenter=True)
                        self._sculpted()
                    return
                self.sculptor.snapshot()
                self._dab(hit[0])
                self._drag = ("brush", e.x, e.y)
                return
        self._drag = (kind, e.x, e.y)

    def _motion(self, e):
        if not self._drag or self.mesh is None:
            return
        if self._drag[0] == "cut":
            self._cut[2:] = [e.x, e.y]
            self.redraw()
            return
        if self._drag[0] == "brush":
            hit = self._pick(e.x, e.y)
            self._brush_at = hit
            if hit is not None:
                self._dab(hit[0])
            return
        kind, x0, y0 = self._drag
        dx, dy = e.x - x0, e.y - y0
        if self.mode != "gl":                  # anteprima statica: un quarto di giro ogni 80 px
            if abs(dx) >= 80:
                self._drag = (kind, e.x, e.y)
                self.static_idx = (self.static_idx + (1 if dx > 0 else -1)) % 4
                self.redraw()
            return
        self._drag = (kind, e.x, e.y)
        if kind == "rotate":
            self.az = (self.az - dx * 0.45) % 360
            self.el = float(np.clip(self.el + dy * 0.45, -89, 89))
        else:
            h = max(self.surface.winfo_height(), 1)
            k = 2 * self.dist * math.tan(math.radians(15)) / h
            a, el = math.radians(self.az), math.radians(self.el)
            right = np.array([math.cos(a), math.sin(a), 0])
            up = np.array([-math.sin(el) * math.sin(a), math.sin(el) * math.cos(a), math.cos(el)])
            self.target = self.target - right * dx * k + up * dy * k
        self.redraw()

    # ------------------------------------------------------------ ritocchi a mano
    def start_tool(self, tool):
        """Strumento di ritocco: trascina sul modello per usarlo, fuori dal modello per
        girare la vista. Il modello resta in memoria finché non si chiude lo strumento."""
        if self.mesh is None or self.mode != "gl":
            return False
        if self.quad:
            self.set_quad(False)
        self.cancel_cut()
        if self.sculptor is None:
            import sculpt
            V, F, _ = self.mesh
            self.sculptor = sculpt.Sculptor(V, F)
        self.tool = tool
        self.surface.config(cursor="circle" if tool != "base" else "target")
        self.redraw()
        return True

    def stop_tool(self):
        self.tool, self._brush_at = None, None
        self.sculptor = None
        self.surface.config(cursor="")
        if self.mesh is not None and self.gl is not None:
            self._upload()                      # di nuovo la mesh fissa sulla scheda video
        self.redraw()

    def sculpt_rotate(self, axis, degrees):
        if self.sculptor is None and not self.start_tool("smooth"):
            return
        self.sculptor.rotate(axis, degrees)
        self._refresh_mesh(recenter=True)
        self._sculpted()

    def sculpt_undo(self):
        if self.sculptor and self.sculptor.undo_last():
            self._refresh_mesh(recenter=True)
            self._sculpted()
            return True
        return False

    def _pick(self, x, y):
        if self.sculptor is None or self.bounds is None:
            return None
        a, b = self._ray(x, y)
        size = float(np.linalg.norm(self.bounds[1] - self.bounds[0]))
        p = self.sculptor.pick(a, b, max(0.25 * self.brush_mm, 0.004 * size))
        if p is None:
            return None
        k = np.argmin(np.linalg.norm(self.sculptor.V - p, axis=1))
        return p, self.sculptor.N[k].astype(float)

    def _dab(self, c):
        self.sculptor.brush(c, self.brush_mm, self.brush_strength, self.tool)
        self._refresh_mesh()

    def _refresh_mesh(self, recenter=False):
        s = self.sculptor
        self.mesh = (s.V, s.F, s.N)
        if self.colors is not None and len(self.colors) != len(s.V):
            self.colors = None                   # forma cambiata (base): colori non più validi
        if recenter:
            self.bounds = (s.V.min(0), s.V.max(0))
        self.redraw()

    def _sculpted(self):
        V, F, _ = self.mesh
        if self.on_sculpt:
            self.on_sculpt(self.sculptor, mesh_info(V, F))

    def _draw_brush(self):
        if not self._brush_at or (self._drag and self._drag[0] != "brush"):
            return
        gl = self.gl
        p, n = self._brush_at
        u = np.cross(n, [0, 0, 1] if abs(n[2]) < 0.9 else [1, 0, 0])
        u /= np.linalg.norm(u)
        v = np.cross(n, u)
        r = self.brush_mm
        gl.Disable(GL_DEPTH_TEST)
        gl.LineWidth(2.0)
        gl.Color4f(*((1.0, 0.62, 0.25, 0.95) if self.tool == "base" else (0.42, 0.65, 1.0, 0.95)))
        gl.Begin(GL_LINE_LOOP)
        for t in np.linspace(0, 2 * np.pi, 48, endpoint=False):
            q = p + n * 0.2 + r * (np.cos(t) * u + np.sin(t) * v)
            gl.Vertex3f(*map(float, q))
        gl.End()
        gl.LineWidth(1.0)
        gl.Enable(GL_DEPTH_TEST)

    def _call_mesh(self):
        """Mesh fissa (lista sulla scheda video) o, mentre si scolpisce, quella che cambia."""
        if self.tool and self.sculptor is not None:
            self._emit_mesh()
        else:
            self.gl.CallList(self._list)

    def _emit_mesh(self):
        gl = self.gl
        V, F, N = self.mesh
        gl.EnableClientState(GL_VERTEX_ARRAY)
        gl.EnableClientState(GL_NORMAL_ARRAY)
        gl.VertexPointer(3, GL_FLOAT, 0, V.ctypes.data)
        gl.NormalPointer(GL_FLOAT, 0, N.ctypes.data)
        C = self.colors
        if C is not None:                         # colori delle foto sui vertici
            gl.Enable(GL_COLOR_MATERIAL)
            gl.ColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
            gl.EnableClientState(GL_COLOR_ARRAY)
            gl.ColorPointer(3, GL_UNSIGNED_BYTE, 0, C.ctypes.data)
        gl.DrawElements(GL_TRIANGLES, F.size, GL_UNSIGNED_INT, F.ctypes.data)
        if C is not None:
            gl.DisableClientState(GL_COLOR_ARRAY)
            gl.Disable(GL_COLOR_MATERIAL)
        gl.DisableClientState(GL_NORMAL_ARRAY)
        gl.DisableClientState(GL_VERTEX_ARRAY)

    # ------------------------------------------------------------ disegno
    def redraw(self):
        if not self._pending:
            self._pending = True
            self.after_idle(self._draw)

    def _draw(self):
        self._pending = False
        if self.mode == "gl":
            if self._ensure_gl():
                try:
                    return self._draw_gl()
                except OSError as e:
                    print("OpenGL non funziona, uso l'anteprima statica:", e, file=sys.stderr)
            self._go_static()
        self._draw_static()

    def _go_static(self):
        if self.mode != "static":
            self.mode = "static"
            self.surface.place_forget()
            self.static.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.placeholder.lift()

    def _ensure_gl(self):
        if self.gl is not None:
            return True
        if self.mode != "gl":
            return False
        try:
            u32, g32, ogl = _wgl()
            self._hwnd = self.surface.winfo_id()
            self._hdc = u32.GetDC(self._hwnd)
            pfd = basic_pfd()
            fmt = multisample_format()
            self.antialias = bool(fmt)
            if fmt:
                g32.DescribePixelFormat(self._hdc, fmt, ctypes.sizeof(PFD), ctypes.byref(pfd))
            else:
                fmt = g32.ChoosePixelFormat(self._hdc, ctypes.byref(pfd))
            if not fmt or not g32.SetPixelFormat(self._hdc, fmt, ctypes.byref(pfd)):
                raise OSError("formato dei pixel non accettato")
            self._ctx = ogl.wglCreateContext(self._hdc)
            if not self._ctx or not ogl.wglMakeCurrent(self._hdc, self._ctx):
                raise OSError("contesto OpenGL non creato")
            self._swap, self._wgl = g32.SwapBuffers, ogl
            self.gl = GL()
            self.renderer = (self.gl.GetString(GL_RENDERER) or b"").decode(errors="replace")
            if self.mesh is not None:
                self._upload()
            return True
        except (OSError, AttributeError, ValueError) as e:
            print("OpenGL non disponibile, uso l'anteprima statica:", e, file=sys.stderr)
            self.mode = "failed"
            self.gl = None
            return False

    def _upload(self):
        gl = self.gl
        self._wgl.wglMakeCurrent(self._hdc, self._ctx)
        if self._list:
            gl.DeleteLists(self._list, 1)
        self._list = gl.GenLists(1)
        gl.NewList(self._list, GL_COMPILE)          # la mesh resta sulla scheda video
        self._emit_mesh()
        gl.EndList()

    def _matrices(self, w, h):
        a, el = math.radians(self.az), math.radians(self.el)
        eye = self.target + self.dist * np.array([math.cos(el) * math.sin(a),
                                                  -math.cos(el) * math.cos(a), math.sin(el)])
        near = max(self.dist * 0.02, 0.5)
        proj = perspective(30, w / h, near, self.dist * 20 + PLATE_MM * 4)
        # OpenGL vuole le matrici per colonne: trasposte e tenute in vita durante la chiamata
        return (np.ascontiguousarray(proj.T, np.float32),
                np.ascontiguousarray(look_at(eye, self.target).T, np.float32))

    def _draw_gl(self, swap=True):
        w, h = self.surface.winfo_width(), self.surface.winfo_height()
        if w < 2 or h < 2:
            return
        gl = self.gl
        self._wgl.wglMakeCurrent(self._hdc, self._ctx)
        gl.Viewport(0, 0, w, h)
        gl.ClearColor(*BOTTOM, 1)
        gl.Clear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        gl.Enable(GL_MULTISAMPLE)
        gl.Disable(GL_DEPTH_TEST)
        gl.Disable(GL_LIGHTING)
        gl.MatrixMode(GL_PROJECTION)
        gl.LoadIdentity()
        gl.MatrixMode(GL_MODELVIEW)
        gl.LoadIdentity()
        gl.Begin(GL_QUADS)                          # sfondo sfumato
        gl.Color4f(*BOTTOM, 1)
        gl.Vertex3f(-1, -1, 0)
        gl.Vertex3f(1, -1, 0)
        gl.Color4f(*TOP, 1)
        gl.Vertex3f(1, 1, 0)
        gl.Vertex3f(-1, 1, 0)
        gl.End()
        if self.quad and self.mesh is not None and self._list:
            self._draw_quad(w, h)
            if swap:
                self._swap(self._hdc)
            return
        self._clear_quad_labels()
        if self.mesh is not None and self._list:
            proj, view = self._matrices(w, h)
            gl.MatrixMode(GL_PROJECTION)
            gl.LoadMatrixf(proj.ctypes.data)
            gl.MatrixMode(GL_MODELVIEW)
            gl.LoadIdentity()
            # luci fisse rispetto a chi guarda: principale in alto a sinistra, una fredda a destra
            gl.Lightfv(GL_LIGHT0, GL_POSITION, floats(-0.55, 0.75, 0.42, 0))
            gl.Lightfv(GL_LIGHT0, GL_DIFFUSE, floats(0.88, 0.86, 0.82, 1))
            gl.Lightfv(GL_LIGHT0, GL_SPECULAR, floats(0.25, 0.25, 0.25, 1))
            gl.Lightfv(GL_LIGHT1, GL_POSITION, floats(0.8, -0.25, 0.55, 0))
            gl.Lightfv(GL_LIGHT1, GL_DIFFUSE, floats(0.26, 0.29, 0.36, 1))
            gl.Lightfv(GL_LIGHT1, GL_SPECULAR, floats(0, 0, 0, 1))
            gl.LoadMatrixf(view.ctypes.data)
            gl.Enable(GL_DEPTH_TEST)
            gl.Enable(GL_BLEND)
            gl.BlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            self._draw_plate()
            gl.Enable(GL_LIGHTING)
            gl.Enable(GL_LIGHT0)
            gl.Enable(GL_LIGHT1)
            gl.Enable(GL_NORMALIZE)
            gl.ShadeModel(GL_SMOOTH)
            gl.LightModelfv(GL_LIGHT_MODEL_AMBIENT, floats(0.15, 0.15, 0.17, 1))
            gl.LightModeli(GL_LIGHT_MODEL_TWO_SIDE, 1)
            gl.Materialfv(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE, floats(*MODEL_RGB, 1))
            gl.Materialfv(GL_FRONT_AND_BACK, GL_SPECULAR, floats(0.22, 0.22, 0.22, 1))
            gl.Materialf(GL_FRONT_AND_BACK, GL_SHININESS, 24)
            self._call_mesh()
            gl.Disable(GL_LIGHTING)
            if self.tool:
                self._draw_brush()
        if self.cut_mode:
            self._draw_cut_overlay(w, h)
        if swap:
            self._swap(self._hdc)

    # ------------------------------------------------------------ griglia 2×2
    def _clear_quad_labels(self):
        """Nasconde le etichette (restano pronte per il disegno successivo)."""
        for lab in self.quad_labels:
            lab.place_forget()
        self._label_used = 0

    def _label(self, text, x, y, anchor="center", big=False):
        k = getattr(self, "_label_used", 0)
        if k == len(self.quad_labels):
            self.quad_labels.append(tk.Label(self, padx=5, pady=1, bd=0))
        lab = self.quad_labels[k]
        self._label_used = k + 1
        lab.config(text=text, bg="#1b1d22" if big else "#24262c",
                   fg="#e9ebef" if big else "#9aa0aa",
                   font=("Segoe UI Semibold", 10) if big else ("Segoe UI", 9))
        lab.place(x=x, y=y, anchor=anchor)
        tk.Misc.lift(lab)

    def _setup_light(self):
        gl = self.gl
        gl.Lightfv(GL_LIGHT0, GL_POSITION, floats(-0.55, 0.75, 0.42, 0))
        gl.Lightfv(GL_LIGHT0, GL_DIFFUSE, floats(0.88, 0.86, 0.82, 1))
        gl.Lightfv(GL_LIGHT0, GL_SPECULAR, floats(0.25, 0.25, 0.25, 1))
        gl.Lightfv(GL_LIGHT1, GL_POSITION, floats(0.8, -0.25, 0.55, 0))
        gl.Lightfv(GL_LIGHT1, GL_DIFFUSE, floats(0.26, 0.29, 0.36, 1))
        gl.Lightfv(GL_LIGHT1, GL_SPECULAR, floats(0, 0, 0, 1))

    def _draw_model(self):
        gl = self.gl
        gl.Enable(GL_LIGHTING)
        gl.Enable(GL_LIGHT0)
        gl.Enable(GL_LIGHT1)
        gl.Enable(GL_NORMALIZE)
        gl.ShadeModel(GL_SMOOTH)
        gl.LightModelfv(GL_LIGHT_MODEL_AMBIENT, floats(0.15, 0.15, 0.17, 1))
        gl.LightModeli(GL_LIGHT_MODEL_TWO_SIDE, 1)
        gl.Materialfv(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE, floats(*MODEL_RGB, 1))
        gl.Materialfv(GL_FRONT_AND_BACK, GL_SPECULAR, floats(0.22, 0.22, 0.22, 1))
        gl.Materialf(GL_FRONT_AND_BACK, GL_SHININESS, 24)
        self._call_mesh()
        gl.Disable(GL_LIGHTING)

    def _draw_quad(self, w, h):
        """Quattro riquadri: Davanti, Destra, Alto (ortografiche, griglia da 1 cm, sagoma del
        piatto e quote in mm) e la vista 3D. Stessa scala per le tre viste quotate."""
        gl = self.gl
        self._clear_quad_labels()
        lo, hi = (np.asarray(b, float) for b in self.bounds)
        size, mid = hi - lo, (lo + hi) / 2
        hw, hh = w // 2, h // 2
        # scala comune: il lato più lungo occupa al massimo il 62% del riquadro più stretto
        mm_per_px = max(size.max() / (0.62 * min(hw, hh)), 1e-6)
        cells = ((0, hh, "Davanti"), (hw, hh, "Destra"), (0, 0, "Alto"), (hw, 0, "3D"))
        for x0, y0, name in cells:
            vw, vh = (w - hw if x0 else hw), (h - hh if y0 else hh)
            gl.Viewport(x0, y0, vw, vh)
            gl.Enable(GL_DEPTH_TEST)
            gl.Clear(GL_DEPTH_BUFFER_BIT)
            top_left = (x0, h - (y0 + vh))                     # in coordinate Tk
            if name == "3D":
                proj, view = self._matrices(vw, vh)
                gl.MatrixMode(GL_PROJECTION)
                gl.LoadMatrixf(proj.ctypes.data)
                gl.MatrixMode(GL_MODELVIEW)
                gl.LoadIdentity()
                self._setup_light()
                gl.LoadMatrixf(view.ctypes.data)
                gl.Enable(GL_BLEND)
                gl.BlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
                self._draw_plate()
                self._draw_model()
            else:
                spec = next(o for o in ORTHO if o[0] == name)
                self._draw_ortho(spec, vw, vh, mm_per_px, lo, hi, mid, top_left)
            self._label(name.upper(), top_left[0] + vw - 10, top_left[1] + 10, "ne")
        # linee di separazione fra i riquadri
        gl.Viewport(0, 0, w, h)
        gl.Disable(GL_DEPTH_TEST)
        gl.MatrixMode(GL_PROJECTION)
        gl.LoadIdentity()
        gl.MatrixMode(GL_MODELVIEW)
        gl.LoadIdentity()
        gl.LineWidth(2.0)
        gl.Color4f(0.09, 0.09, 0.11, 1)
        gl.Begin(GL_LINES)
        cx, cy = 2 * hw / w - 1, 2 * hh / h - 1
        gl.Vertex3f(cx, -1, 0)
        gl.Vertex3f(cx, 1, 0)
        gl.Vertex3f(-1, cy, 0)
        gl.Vertex3f(1, cy, 0)
        gl.End()
        gl.LineWidth(1.0)

    def _draw_ortho(self, spec, vw, vh, mm_per_px, lo, hi, mid, top_left):
        gl = self.gl
        name, look, up, ax, ay = spec
        look, up = np.asarray(look, float), np.asarray(up, float)
        half_w, half_h = vw * mm_per_px / 2, vh * mm_per_px / 2
        depth = PLATE_MM * 4
        proj = orthographic(half_w, half_h, depth)
        view = look_at(mid + look * PLATE_MM * 2, mid, up)
        gl.MatrixMode(GL_PROJECTION)
        gl.LoadMatrixf(np.ascontiguousarray(proj.T, np.float32).ctypes.data)
        gl.MatrixMode(GL_MODELVIEW)
        gl.LoadIdentity()
        self._setup_light()
        vt = np.ascontiguousarray(view.T, np.float32)
        gl.LoadMatrixf(vt.ctypes.data)
        # griglia da 1 cm sul piano del disegno, dietro al modello
        back = mid - look * (np.dot(hi - lo, np.abs(look)) / 2 + 1)
        ex, ey = np.eye(3)[ax], np.eye(3)[ay]
        gl.Enable(GL_BLEND)
        gl.BlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        gl.DepthMask(0)
        gl.Begin(GL_LINES)
        span = max(half_w, half_h) * 1.2
        n = int(span // 10) + 1
        c0 = np.round(mid / 10) * 10
        for i in range(-n, n + 1):
            gl.Color4f(0.34, 0.36, 0.42, 0.55 if i % 5 == 0 else 0.22)
            for e1, e2 in ((ex, ey), (ey, ex)):
                o = back + e1 * (c0 @ e1 - mid @ e1 + i * 10)
                a, b = o - e2 * span, o + e2 * span
                gl.Vertex3f(*a)
                gl.Vertex3f(*b)
        gl.End()
        # sagoma del piatto della stampante (256 mm)
        plate_c = np.array([mid[0], mid[1], lo[2]])
        hp = PLATE_MM / 2
        gl.Color4f(0.38, 0.62, 0.98, 0.7)
        gl.LineWidth(1.5)
        gl.Begin(GL_LINE_LOOP)
        if name == "Alto":
            for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                gl.Vertex3f(plate_c[0] + sx * hp, plate_c[1] + sy * hp, lo[2] - 0.5)
        else:
            e = ex
            p0 = plate_c - e * hp
            p1 = plate_c + e * hp
            gl.Vertex3f(*(p0 - look * 0))
            gl.Vertex3f(*p1)
            gl.Vertex3f(*(p1 - np.array([0, 0, 1.5])))
            gl.Vertex3f(*(p0 - np.array([0, 0, 1.5])))
        gl.End()
        gl.LineWidth(1.0)
        gl.DepthMask(1)
        self._draw_model()

        # quote: orizzontale sotto il modello, verticale a sinistra
        gl.Disable(GL_DEPTH_TEST)
        front = mid + look * (np.dot(hi - lo, np.abs(look)) / 2 + 2)

        def pt(u, v):                                   # punto del disegno in mm
            return front + ex * (u - front @ ex) + ey * (v - front @ ey)
        u0, u1, v0, v1 = lo[ax], hi[ax], lo[ay], hi[ay]
        gap = 14 * mm_per_px
        tick = 5 * mm_per_px
        gl.Color4f(0.38, 0.62, 0.98, 1)
        gl.LineWidth(1.5)
        gl.Begin(GL_LINES)
        for a, b in ((pt(u0, v0 - gap), pt(u1, v0 - gap)), (pt(u0 - gap, v0), pt(u0 - gap, v1))):
            gl.Vertex3f(*a)
            gl.Vertex3f(*b)
        for u in (u0, u1):                              # linee di richiamo e tacche
            gl.Vertex3f(*pt(u, v0 - gap - tick))
            gl.Vertex3f(*pt(u, v0 - 2 * mm_per_px))
        for v in (v0, v1):
            gl.Vertex3f(*pt(u0 - gap - tick, v))
            gl.Vertex3f(*pt(u0 - 2 * mm_per_px, v))
        gl.End()
        gl.LineWidth(1.0)

        def to_px(p):                                   # da mm a pixel Tk del riquadro
            c = proj @ view @ np.append(p, 1)
            return (top_left[0] + (c[0] / c[3] + 1) / 2 * vw,
                    top_left[1] + (1 - c[1] / c[3]) / 2 * vh)
        x, y = to_px(pt((u0 + u1) / 2, v0 - gap))
        self._label(f"{u1 - u0:.1f} mm", x, y + 12, big=True)
        x, y = to_px(pt(u0 - gap, (v0 + v1) / 2))
        self._label(f"{v1 - v0:.1f} mm", x - 8, y, "e", big=True)

    def _draw_plate(self):
        """Piatto di stampa con griglia da 1 cm e ombra morbida sotto il modello."""
        gl = self.gl
        lo, hi = self.bounds
        half = PLATE_MM / 2
        cx, cy, z = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, float(lo[2])
        gl.DepthMask(0)
        gl.Begin(GL_QUADS)
        gl.Color4f(0.16, 0.17, 0.20, 0.95)
        for x, y in ((-half, -half), (half, -half), (half, half), (-half, half)):
            gl.Vertex3f(cx + x, cy + y, z)
        gl.End()
        gl.LineWidth(1.0)
        gl.Begin(GL_LINES)
        n = int(half) // 10
        for i in range(-n, n + 1):
            gl.Color4f(0.32, 0.34, 0.40, 0.85 if i % 5 == 0 else 0.35)
            t = i * 10.0
            gl.Vertex3f(cx + t, cy - half, z)
            gl.Vertex3f(cx + t, cy + half, z)
            gl.Vertex3f(cx - half, cy + t, z)
            gl.Vertex3f(cx + half, cy + t, z)
        gl.End()
        rx, ry = (hi[0] - lo[0]) * 0.62, (hi[1] - lo[1]) * 0.62
        gl.Begin(GL_TRIANGLE_FAN)                   # ombra di contatto
        gl.Color4f(0, 0, 0, 0.5)
        gl.Vertex3f(cx, cy, z)
        gl.Color4f(0, 0, 0, 0)
        for k in range(49):
            t = 2 * math.pi * k / 48
            gl.Vertex3f(cx + rx * math.cos(t), cy + ry * math.sin(t), z)
        gl.End()
        gl.DepthMask(1)

    def snapshot(self):
        """Immagine (PIL) della vista attuale, per salvarla."""
        from PIL import Image
        if self.mode != "gl" or self.gl is None:
            img = self.static_views[self.static_idx % 4] if self.static_views else None
            return img.copy() if img else None
        w, h = self.surface.winfo_width(), self.surface.winfo_height()
        self._draw_gl(swap=False)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        self.gl.PixelStorei(GL_PACK_ALIGNMENT, 1)
        self.gl.ReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, ctypes.addressof(buf))
        self._swap(self._hdc)
        return Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "RGBA", 0, -1).convert("RGB")

    # ------------------------------------------------------------ senza OpenGL
    def _load_static(self, preview):
        self.static_views = []
        try:
            from PIL import Image
            arr = np.array(Image.open(preview).convert("RGB"))
            bg = [int(self.bg[i:i + 2], 16) for i in (1, 3, 5)]
            arr[(arr >= 254).all(-1)] = bg                 # sfondo bianco -> scuro
            img = Image.fromarray(arr)
            q = img.width // 4
            self.static_views = [img.crop((i * q, 0, (i + 1) * q, img.height)) for i in range(4)]
        except (OSError, ImportError, ValueError):
            self.message("Anteprima non disponibile.")

    def _draw_static(self):
        if not self.static_views:
            return
        from PIL import ImageTk
        img = self.static_views[self.static_idx % 4]
        w, h = max(self.static.winfo_width(), 50), max(self.static.winfo_height(), 50)
        k = min(w / img.width, h / img.height)
        img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))))
        self._photo = ImageTk.PhotoImage(img)
        self.static.config(image=self._photo)

    def _destroy(self, e):
        if e.widget is not self or self.gl is None:
            return
        try:
            self._wgl.wglMakeCurrent(None, None)
            self._wgl.wglDeleteContext(self._ctx)
            ctypes.windll.user32.ReleaseDC(ctypes.c_void_p(self._hwnd), ctypes.c_void_p(self._hdc))
        except OSError:
            pass
        self.gl = None
