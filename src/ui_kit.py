"""Tema scuro e widget arrotondati dell'interfaccia (ttk + immagini disegnate con PIL),
più le integrazioni con Windows: barra del titolo scura, trascina e rilascia dei file,
pulsante lampeggiante nella barra delle applicazioni a lavoro finito."""
import ctypes
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

WIN = sys.platform == "win32"

# palette in stile applicazioni di grafica professionali (grigi neutri, accento blu)
BG = "#1b1b1b"          # finestra e fessure fra i pannelli
SIDE = "#222222"        # barre dei titoli dei pannelli
CARD = "#2b2b2b"        # pannelli
FIELD = "#1f1f1f"       # campi (più scuri del pannello)
HOVER = "#3d3d3d"
LINE = "#3a3a3a"
FG = "#d6d6d6"
MUTED = "#9c9c9c"
FAINT = "#6a6a6a"
DROP = "#252525"        # zona dove trascinare le foto
ACCENT = "#378ef0"
ACCENT_HI = "#4b9df7"
ACCENT_LO = "#1f4b7c"
OK = "#4fbf7f"
WARN = "#e8b84a"
ERR = "#e5534b"
RADIUS = 2              # angoli quasi squadrati
BUTTON = "#3a3a3a"      # pulsanti normali


def pick_font(root, *names):
    have = set(tkfont.families(root))
    return next((n for n in names if n in have), "TkDefaultFont")


# sfondi su cui stanno i widget: ogni stile esiste in una variante per sfondo, così gli
# angoli arrotondati delle immagini si fondono con quello che c'è dietro
BGS = {"": BG, "Side.": SIDE, "Card.": CARD, "Drop.": DROP}


def hex_rgba(color, alpha=255):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) + (alpha,)


class Theme:
    """Stili ttk dell'applicazione. Le immagini (angoli arrotondati antialias) sono
    generate alla risoluzione reale dello schermo e tenute in self.images."""

    def __init__(self, root, scale=1.0):
        self.root, self.k = root, scale
        self.images = {}
        self.ui = pick_font(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica")
        self.ui_semi = pick_font(root, "Segoe UI Variable Display Semib", "Segoe UI Semibold",
                                 "Segoe UI", "Helvetica")
        self.mono = pick_font(root, "Cascadia Mono", "Consolas", "Courier New")
        self.f = {"base": (self.ui, 9), "small": (self.ui, 8), "tiny": (self.ui, 8),
                  "head": (self.ui_semi, 8), "title": (self.ui_semi, 10),
                  "big": (self.ui_semi, 10), "value": (self.ui_semi, 9),
                  "mono": (self.mono, 9)}
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self._build()

    def px(self, v):
        return max(1, int(round(v * self.k)))

    # ------------------------------------------------------------ immagini
    def rounded(self, w, h, r, fill, outline=None, width=1.0, bg=None, ss=4):
        """Rettangolo arrotondato (dimensioni in px logici) come PhotoImage."""
        r = min(r, RADIUS)
        W, H = self.px(w), self.px(h)
        img = Image.new("RGBA", (W * ss, H * ss), hex_rgba(bg) if bg else (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        lw = int(round(width * self.k * ss)) if outline else 0
        d.rounded_rectangle((0, 0, W * ss - 1, H * ss - 1), self.px(r) * ss,
                            fill=hex_rgba(fill) if fill else None,
                            outline=hex_rgba(outline) if outline else None, width=lw)
        return img.resize((W, H), Image.LANCZOS)

    def photo(self, key, pil):
        self.images[key] = ImageTk.PhotoImage(pil)
        return self.images[key]

    def element(self, name, base, states=(), border=8, sticky="nsew", **kw):
        """Crea un elemento ttk a immagine (9 parti: gli angoli non si deformano)."""
        imgs = [self.photo(f"{name}:0", base)]
        specs = []
        for i, (state, pil) in enumerate(states, 1):
            specs.append((state, self.photo(f"{name}:{i}", pil)))
        if isinstance(border, (tuple, list)):
            border = tuple(self.px(b) if b else 0 for b in border)
        else:
            border = self.px(border) if border else 0
        self.style.element_create(name, "image", imgs[0], *specs,
                                  border=border, sticky=sticky, **kw)

    # ------------------------------------------------------------ stili
    def _build(self):
        s, f, px = self.style, self.f, self.px
        s.configure(".", background=BG, foreground=FG, font=f["base"], borderwidth=0,
                    focuscolor=ACCENT, troughcolor=FIELD, fieldbackground=FIELD,
                    selectbackground=ACCENT, selectforeground="white", insertcolor=FG)
        for prefix, bg in BGS.items():                  # riquadri semplici
            s.configure(f"{prefix}TFrame", background=bg)
        # schede arrotondate (sfondo della finestra negli angoli)
        self.element("Panel.bg", self.rounded(30, 30, 12, CARD, bg=BG), border=13)
        s.layout("Panel.TFrame", [("Panel.bg", {"sticky": "nsew"})])
        s.configure("Panel.TFrame", background=CARD)
        # etichette
        for prefix, bg in BGS.items():
            s.configure(f"{prefix}TLabel", background=bg, foreground=FG, font=f["base"])
            s.configure(f"{prefix}Muted.TLabel", background=bg, foreground=MUTED, font=f["small"])
            s.configure(f"{prefix}Faint.TLabel", background=bg, foreground=FAINT, font=f["small"])
            s.configure(f"{prefix}Head.TLabel", background=bg, foreground=FG, font=f["head"])
            s.configure(f"{prefix}Value.TLabel", background=bg, foreground=FG, font=f["value"])
            s.configure(f"{prefix}Ok.TLabel", background=bg, foreground=OK, font=f["small"])
            s.configure(f"{prefix}Warn.TLabel", background=bg, foreground=WARN, font=f["small"])
            s.configure(f"{prefix}Err.TLabel", background=bg, foreground=ERR, font=f["small"])
        s.configure("Title.TLabel", background=BG, foreground=FG, font=f["title"])
        for prefix in BGS:                       # titoli di sezione: maiuscolo grigio
            s.configure(f"{prefix}Head.TLabel", foreground=MUTED)

        # pulsanti: varianti per ogni sfondo su cui stanno (angoli puliti)
        def button(kind, fill, hover, press, fg, font, pad, disabled=(FIELD, FAINT)):
            for prefix, bg in BGS.items():
                el = f"{prefix}{kind}.bg"
                self.element(el, self.rounded(24, 24, 8, fill, bg=bg),
                             [("disabled", self.rounded(24, 24, 8, disabled[0], bg=bg)),
                              ("pressed", self.rounded(24, 24, 8, press, bg=bg)),
                              ("active", self.rounded(24, 24, 8, hover, bg=bg))], border=9)
                st = f"{prefix}{kind}.TButton"
                s.layout(st, [(el, {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [
                        ("Button.label", {"sticky": "nsew"})]})]})])
                s.configure(st, background=bg, foreground=fg, font=font, padding=pad,
                            anchor="center", width=0)        # clam impone 11 caratteri minimi
                s.map(st, foreground=[("disabled", disabled[1])])
        button("Accent", ACCENT, ACCENT_HI, "#2b78d0", "white", f["big"], (px(14), px(8)),
               disabled=(ACCENT_LO, "#8d9bbd"))
        button("Small", BUTTON, "#474747", "#303030", FG, f["base"], (px(10), px(5)))
        button("Primary", ACCENT, ACCENT_HI, "#2b78d0", "white", f["value"], (px(12), px(6)),
               disabled=(FIELD, FAINT))
        button("Danger", "#5a2a31", "#743540", "#4a2228", "#ffd9dc", f["big"], (px(16), px(10)))
        button("Ghost", None, FIELD, HOVER, MUTED, f["base"], (px(8), px(5)),
               disabled=(None, FAINT))
        button("Tool", None, FIELD, HOVER, MUTED, f["small"], (px(8), px(4)),
               disabled=(None, FAINT))
        for prefix in BGS:
            for kind in ("Ghost", "Tool"):
                s.map(f"{prefix}{kind}.TButton", foreground=[("disabled", FAINT), ("active", FG)])

        # campi di testo
        self.element("Field.bg", self.rounded(24, 24, 7, FIELD, bg=CARD),
                     [("focus", self.rounded(24, 24, 7, FIELD, ACCENT, 1.5, bg=CARD)),
                      ("disabled", self.rounded(24, 24, 7, CARD, LINE, 1, bg=CARD))], border=8)
        s.layout("Field.TEntry", [("Field.bg", {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [
                ("Entry.textarea", {"sticky": "nsew"})]})]})])
        s.configure("Field.TEntry", padding=(px(9), px(5)), foreground=FG,
                    fieldbackground=FIELD, insertcolor=FG)

        # interruttore on/off (Checkbutton)
        def switch(on, hover=False, disabled=False):
            """Casella di spunta quadrata."""
            S, ss = self.px(14), 4
            img = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            fill = (ACCENT_HI if hover else ACCENT) if on else FIELD
            edge = fill if on else ("#5a5a5a" if hover else "#4a4a4a")
            if disabled:
                fill, edge = (ACCENT_LO if on else CARD), LINE
            d.rounded_rectangle((0, 0, S * ss - 1, S * ss - 1), 2 * ss, fill=hex_rgba(fill),
                                outline=hex_rgba(edge), width=ss)
            if on:
                c = S * ss
                d.line([(c * 0.24, c * 0.52), (c * 0.43, c * 0.70), (c * 0.77, c * 0.31)],
                       fill=hex_rgba("#ffffff"), width=max(ss, int(1.8 * ss * self.k)),
                       joint="curve")
            return img.resize((S, S), Image.LANCZOS)
        for prefix, bg in BGS.items():
            if prefix == "":
                self.element("Switch.ind", switch(False),
                             [(("selected", "disabled"), switch(True, disabled=True)),
                              ("disabled", switch(False, disabled=True)),
                              (("selected", "active"), switch(True, hover=True)),
                              ("selected", switch(True)), ("active", switch(False, True))],
                             border=0, sticky="")
            st = f"{prefix}Switch.TCheckbutton"
            s.layout(st, [("Checkbutton.padding", {"sticky": "nsew", "children": [
                ("Switch.ind", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "w"})]})])
            s.configure(st, background=bg, foreground=FG, padding=(0, px(2)), font=f["base"])
            s.map(st, background=[("active", bg)], foreground=[("disabled", FAINT)])

        # controllo a segmenti (Radiobutton)
        for prefix, bg in BGS.items():
            el = f"{prefix}Seg.bg"
            self.element(el, self.rounded(24, 24, 7, FIELD, bg=bg),
                         [(("selected", "disabled"), self.rounded(24, 24, 7, ACCENT_LO, bg=bg)),
                          ("disabled", self.rounded(24, 24, 7, CARD, LINE, 1, bg=bg)),
                          ("selected", self.rounded(24, 24, 7, ACCENT, bg=bg)),
                          ("active", self.rounded(24, 24, 7, HOVER, bg=bg))], border=8)
            st = f"{prefix}Seg.TRadiobutton"
            s.layout(st, [(el, {"sticky": "nsew", "children": [
                ("Radiobutton.padding", {"sticky": "nsew", "children": [
                    ("Radiobutton.label", {"sticky": "nsew"})]})]})])
            s.configure(st, background=bg, foreground=MUTED, padding=(px(6), px(6)),
                        anchor="center", justify="center", font=f["small"])
            s.map(st, foreground=[("disabled", FAINT), ("selected", "white"), ("active", FG)],
                  background=[("active", bg)])

        # cursore: binario sottile e pomello rotondo
        def knob(color, ring=None):
            S, ss = self.px(12), 4
            img = Image.new("RGBA", (S * ss, S * ss), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((0, 0, S * ss - 1, S * ss - 1), fill=hex_rgba(ring or color))
            m = int(S * ss * 0.12)
            d.ellipse((m, m, S * ss - 1 - m, S * ss - 1 - m), fill=hex_rgba("#e8e8e8"))
            return img.resize((S, S), Image.LANCZOS)

        def trough(color):
            W, H, ss = self.px(24), self.px(18), 4
            img = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
            t = self.px(2) * ss
            y0 = (H * ss - t) // 2
            ImageDraw.Draw(img).rounded_rectangle((0, y0, W * ss - 1, y0 + t), t // 2,
                                                  fill=hex_rgba(color))
            return img.resize((W, H), Image.LANCZOS)
        self.element("Slim.trough", trough(LINE), [("disabled", trough(FIELD))],
                     border=(4, 0), sticky="ew")
        self.element("Slim.slider", knob(ACCENT), [("disabled", knob(LINE)),
                                                   ("pressed", knob(ACCENT_HI)),
                                                   ("active", knob(ACCENT_HI))],
                     border=0, sticky="")
        for prefix, bg in BGS.items():
            st = f"{prefix}Slim.Horizontal.TScale"
            s.layout(st, [("Slim.trough", {"sticky": "ew", "children": [
                ("Slim.slider", {"side": "left", "sticky": ""})]})])
            s.configure(st, background=bg)

        # barra di avanzamento arrotondata
        self.element("Bar.trough", self.rounded(16, 8, 4, FIELD, bg=BG), border=(5, 3))
        self.element("Bar.pbar", self.rounded(16, 8, 4, ACCENT), border=(5, 3))
        s.layout("Bar.Horizontal.TProgressbar", [("Bar.trough", {"sticky": "nsew", "children": [
            ("Bar.pbar", {"side": "left", "sticky": "ns"})]})])
        s.configure("Bar.Horizontal.TProgressbar", thickness=px(4), background=BG)

        # menù a tendina: normale (progetti) e piccolo con testo grigio ("Cambia ▾")
        for prefix, bg in BGS.items():
            el = f"{prefix}Menu.bg"
            self.element(el, self.rounded(24, 24, 8, FIELD, bg=bg),
                         [("disabled", self.rounded(24, 24, 8, CARD, bg=bg)),
                          ("active", self.rounded(24, 24, 8, HOVER, bg=bg))], border=9)
            for kind, fg, font, pad in (("Pill", FG, f["base"], (px(12), px(6))),
                                        ("Link", MUTED, f["small"], (px(9), px(4)))):
                st = f"{prefix}{kind}.TMenubutton"
                s.layout(st, [(el, {"sticky": "nsew", "children": [
                    ("Menubutton.padding", {"sticky": "nsew", "children": [
                        ("Menubutton.label", {"side": "left", "sticky": ""})]})]})])
                s.configure(st, background=bg, foreground=fg, padding=pad, font=font)
                s.map(st, foreground=[("disabled", FAINT), ("active", FG)])

        # testo del registro e barre di scorrimento sottili
        s.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG, arrowcolor=MUTED,
                    bordercolor=BG, lightcolor=FIELD, darkcolor=FIELD, gripcount=0,
                    arrowsize=px(12))
        s.map("Vertical.TScrollbar", background=[("active", HOVER)])
        s.configure("TSeparator", background=LINE)
        s.configure("Side.TSeparator", background=LINE)


# ----------------------------------------------------------------- widget composti
class Stepper(ttk.Frame):
    """Fasi del lavoro: pallini numerati collegati da una linea (fatto / in corso / da fare)."""

    def __init__(self, master, theme, steps, bg=BG):
        super().__init__(master, style="TFrame")
        self.t, self.steps, self.bg = theme, steps, bg
        self.icons, self.labels, self.links = [], [], []
        self.state = (-1, False)
        for i, name in enumerate(steps):
            col = 2 * i
            icon = tk.Label(self, bg=bg, bd=0)
            icon.grid(row=0, column=col, padx=theme.px(2))
            lab = tk.Label(self, text=name, bg=bg, fg=MUTED, font=theme.f["small"])
            lab.grid(row=1, column=col, padx=theme.px(4))
            self.icons.append(icon)
            self.labels.append(lab)
            if i < len(steps) - 1:
                link = tk.Frame(self, bg=LINE, height=theme.px(2))
                link.grid(row=0, column=col + 1, sticky="ew")
                self.columnconfigure(col + 1, weight=1)
                self.links.append(link)
        self._make_icons()
        self.set(-1)

    def _make_icons(self):
        px, ss = self.t.px, 4
        S = px(26)
        try:
            fnt = ImageFont.truetype("seguisb.ttf", int(S * ss * 0.48))
        except OSError:
            fnt = ImageFont.load_default()
        self.cache = {}
        for i in range(len(self.steps)):
            for kind in ("todo", "now", "done", "fail"):
                img = Image.new("RGBA", (S * ss, S * ss), hex_rgba(self.bg))
                d = ImageDraw.Draw(img)
                box = (ss, ss, S * ss - ss, S * ss - ss)
                if kind == "todo":
                    d.ellipse(box, outline=hex_rgba(LINE), width=px(2) * ss, fill=hex_rgba(self.bg))
                    color = MUTED
                else:
                    fill = {"now": ACCENT, "done": OK, "fail": ERR}[kind]
                    d.ellipse(box, fill=hex_rgba(fill))
                    color = "#ffffff"
                c = S * ss / 2
                if kind == "done":                      # segno di spunta
                    pts = [(c - S * ss * 0.2, c + S * ss * 0.01), (c - S * ss * 0.05, c + S * ss * 0.15),
                           (c + S * ss * 0.22, c - S * ss * 0.14)]
                    d.line(pts, fill=hex_rgba(color), width=int(px(2.2) * ss), joint="curve")
                else:
                    text = "!" if kind == "fail" else str(i + 1)
                    l, t, r, b = d.textbbox((0, 0), text, font=fnt)
                    d.text((c - (l + r) / 2, c - (t + b) / 2), text, font=fnt, fill=hex_rgba(color))
                self.cache[i, kind] = ImageTk.PhotoImage(img.resize((S, S), Image.LANCZOS))

    def set(self, current, failed=False):
        """current = indice della fase in corso (len(steps) = tutto fatto, -1 = niente)."""
        self.state = (current, failed)
        for i, (icon, lab) in enumerate(zip(self.icons, self.labels)):
            if i < current:
                kind = "done"
            elif i == current:
                kind = "fail" if failed else "now"
            else:
                kind = "todo"
            icon.config(image=self.cache[i, kind])
            lab.config(fg={"done": FG, "now": FG, "fail": ERR, "todo": MUTED}[kind],
                       font=self.t.f["value"] if kind in ("now", "fail") else self.t.f["small"])
        for i, link in enumerate(self.links):
            link.config(bg=OK if i < current else LINE)


class Tooltip:
    """Suggerimento che compare tenendo il mouse fermo su un widget."""

    def __init__(self, widget, text, theme):
        self.w, self.text, self.t = widget, text, theme
        self.tip, self.job = None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _):
        self.job = self.w.after(550, self._show)

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.w.winfo_rootx() + self.t.px(8)
        y = self.w.winfo_rooty() + self.w.winfo_height() + self.t.px(6)
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=self.text, bg="#30333b", fg=FG, font=self.t.f["small"],
                 justify="left", wraplength=self.t.px(320), padx=self.t.px(10),
                 pady=self.t.px(6)).pack()
        self.tip.wm_geometry(f"+{x}+{y}")

    def _hide(self, _=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Splash(tk.Toplevel):
    """Schermata iniziale con il marchio, il passaggio in corso e una barra di caricamento."""

    def __init__(self, root, image, version=""):
        super().__init__(root)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        with Image.open(image) as im:
            self.img = ImageTk.PhotoImage(im.convert("RGB"))
        w, h = self.img.width(), self.img.height()
        self.cv = tk.Canvas(self, width=w, height=h, highlightthickness=0, bd=0, bg=BG)
        self.cv.pack()
        self.cv.create_image(0, 0, image=self.img, anchor="nw")
        self.w, self.h = w, h
        x0, x1, y = 354, w - 50, h - 70
        self.bar = (x0, x1, y)
        self.cv.create_rectangle(x0, y, x1, y + 3, fill="#33363c", width=0)
        self.fill = self.cv.create_rectangle(x0, y, x0, y + 3, fill=ACCENT, width=0)
        self.msg = self.cv.create_text(x0, y - 14, text="Caricamento", anchor="w", fill=MUTED,
                                       font=("Segoe UI", 9))
        self.pct = self.cv.create_text(x1, y - 14, text="0%", anchor="e", fill=MUTED,
                                       font=("Segoe UI", 9))
        if version:
            self.cv.create_text(w - 16, h - 16, text=version, anchor="se", fill=FAINT,
                                font=("Segoe UI", 8))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        self.shown = 0.0
        self.update()

    def step(self, pct):
        """Fa scorrere la barra fino a pct (0-100)."""
        x0, x1, y = self.bar
        target = max(self.shown, float(pct))
        while self.shown < target:                      # scorrimento morbido
            self.shown = min(target, self.shown + 4)
            self.cv.coords(self.fill, x0, y, x0 + (x1 - x0) * self.shown / 100, y + 3)
            self.cv.itemconfigure(self.pct, text=f"{self.shown:.0f}%")
            self.update()
            self.after(8)


class DarkMenuBar(tk.Frame):
    """Barra dei menu scura (quella di Windows resta bianca e non si può colorare).
    items: [(titolo, funzione che restituisce le voci)]; voce = (testo, comando, scorciatoia)
    oppure None per una linea, oppure (testo, None) per una voce disattivata."""

    def __init__(self, master, theme, menus):
        super().__init__(master, bg=BG)
        self.t, self.popup, self.open_name = theme, None, None
        self.buttons = {}
        for title, items in menus:
            b = tk.Label(self, text=title, bg=BG, fg=FG, font=theme.f["base"],
                         padx=theme.px(9), pady=theme.px(3))
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, t=title, i=items: self.toggle(t, i))
            b.bind("<Enter>", lambda e, t=title, i=items: self._hover(t, i))
            self.buttons[title] = b
        master.winfo_toplevel().bind("<Button-1>", self._click_outside, add="+")
        master.winfo_toplevel().bind("<Configure>", lambda e: self.close(), add="+")

    def _hover(self, title, items):
        b = self.buttons[title]
        if self.popup and self.open_name != title:
            self.toggle(title, items)
        elif not self.popup:
            b.config(bg=HOVER)
            b.bind("<Leave>", lambda e: b.config(bg=BG) if self.open_name != title else None)

    def toggle(self, title, items):
        same = self.open_name == title
        self.close()
        if not same:
            self.open(title, items)

    def open(self, title, items, x=None, y=None):
        b = self.buttons.get(title)
        if b is not None:
            b.config(bg=ACCENT_LO)
            x, y = b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()
        self.open_name = title
        self.popup = self._build(items() if callable(items) else items, x, y)

    def _build(self, items, x, y):
        t = self.t
        pop = tk.Toplevel(self)
        pop.wm_overrideredirect(True)
        pop.attributes("-topmost", True)
        frame = tk.Frame(pop, bg=CARD, highlightthickness=1, highlightbackground="#4a4a4a")
        frame.pack()
        for it in items:
            if it is None:
                tk.Frame(frame, bg=LINE, height=1).pack(fill="x", pady=t.px(3), padx=t.px(6))
                continue
            text, cmd, key = (tuple(it) + (None, None))[:3]
            row = tk.Frame(frame, bg=CARD)
            row.pack(fill="x")
            fg = FG if cmd else FAINT
            a = tk.Label(row, text=text, bg=CARD, fg=fg, font=t.f["base"], anchor="w",
                         padx=t.px(14), pady=t.px(4), width=30)
            a.pack(side="left", fill="x", expand=True)
            k = tk.Label(row, text=key or "", bg=CARD, fg=MUTED, font=t.f["small"],
                         padx=t.px(12))
            k.pack(side="right")
            if not cmd:
                continue
            parts = (row, a, k)

            def enter(_, p=parts):
                for w in p:
                    w.config(bg=ACCENT)
                p[1].config(fg="white")

            def leave(_, p=parts):
                for w in p:
                    w.config(bg=CARD)
                p[1].config(fg=FG)

            def run(_, c=cmd):
                self.close()
                c()
            for w in parts:
                w.bind("<Enter>", enter)
                w.bind("<Leave>", leave)
                w.bind("<ButtonRelease-1>", run)
        pop.wm_geometry(f"+{x}+{y}")
        return pop

    def _click_outside(self, e):
        if not self.popup:
            return
        w = e.widget
        if isinstance(w, tk.Misc) and (str(w).startswith(str(self.popup)) or
                                       w in self.buttons.values()):
            return
        self.close()

    def close(self):
        if self.popup:
            self.popup.destroy()
        self.popup = None
        if self.open_name in self.buttons:
            self.buttons[self.open_name].config(bg=BG)
        self.open_name = None


def thumbnail(path, size, radius, bg):
    """Miniatura quadrata con angoli arrotondati (PIL), None se la foto non si apre."""
    try:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        from PIL import ImageOps
        with Image.open(path) as im:
            im.draft("RGB", (size * 2, size * 2))
            im = ImageOps.exif_transpose(im).convert("RGB")
            im = ImageOps.fit(im, (size, size), Image.LANCZOS)
    except Exception:
        return None
    ss = 4
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * ss - 1, size * ss - 1), radius * ss, fill=255)
    out = Image.new("RGB", (size, size), hex_rgba(bg)[:3])
    out.paste(im, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return out


def glyph(kind, size, color, bg):
    """Piccole icone disegnate: 'photos', 'video', 'drop', 'cube', 'chart'."""
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), hex_rgba(bg, 0))
    d = ImageDraw.Draw(img)
    c = hex_rgba(color)
    w = max(ss * 2, int(S * 0.07))
    if kind == "photos":
        d.rounded_rectangle((S * .22, S * .14, S * .9, S * .7), S * .08, outline=c, width=w)
        d.rounded_rectangle((S * .1, S * .3, S * .78, S * .86), S * .08, fill=hex_rgba(bg),
                            outline=c, width=w)
        d.polygon([(S * .18, S * .78), (S * .38, S * .52), (S * .52, S * .68), (S * .6, S * .6),
                   (S * .72, S * .78)], fill=c)
        d.ellipse((S * .55, S * .38, S * .67, S * .5), fill=c)
    elif kind == "video":
        d.rounded_rectangle((S * .08, S * .26, S * .68, S * .76), S * .08, outline=c, width=w)
        d.polygon([(S * .74, S * .42), (S * .94, S * .3), (S * .94, S * .72), (S * .74, S * .6)], fill=c)
    elif kind == "drop":
        d.line([(S * .5, S * .14), (S * .5, S * .62)], fill=c, width=w)
        d.line([(S * .3, S * .44), (S * .5, S * .64), (S * .7, S * .44)], fill=c, width=w,
               joint="curve")
        d.line([(S * .16, S * .62), (S * .16, S * .84), (S * .84, S * .84), (S * .84, S * .62)],
               fill=c, width=w, joint="curve")
    elif kind == "chart":
        for i, h in enumerate((0.35, 0.6, 0.45, 0.8)):
            x0 = S * (0.1 + i * 0.21)
            d.rounded_rectangle((x0, S * (0.9 - h), x0 + S * 0.15, S * 0.9), S * 0.03, fill=c)
    elif kind == "cube":
        top = [(S * .5, S * .1), (S * .88, S * .3), (S * .5, S * .5), (S * .12, S * .3)]
        d.polygon(top, fill=hex_rgba(ACCENT_HI))
        d.polygon([(S * .12, S * .3), (S * .5, S * .5), (S * .5, S * .92), (S * .12, S * .72)],
                  fill=hex_rgba(ACCENT))
        d.polygon([(S * .88, S * .3), (S * .5, S * .5), (S * .5, S * .92), (S * .88, S * .72)],
                  fill=hex_rgba("#3f6fe0"))
    return img.resize((size, size), Image.LANCZOS)


def symbol(size):
    """Simbolo di Effigie (testa sfaccettata, assets/simbolo.png); un cubo se manca."""
    from pathlib import Path
    png = Path(__file__).resolve().parent.parent / "assets" / "simbolo.png"
    if png.is_file():
        with Image.open(png) as im:
            return im.convert("RGBA").resize((size, size), Image.LANCZOS)
    return glyph("cube", size, ACCENT, BG)


def app_icon(root, theme):
    """Icona della finestra: assets/icona.png (busto con la trama della scansione 3D);
    se manca, un cubo blu disegnato al volo."""
    from pathlib import Path
    png = Path(__file__).resolve().parent.parent / "assets" / "icona.png"
    try:
        if png.is_file():
            with Image.open(png) as im:
                big = ImageTk.PhotoImage(im.resize((64, 64), Image.LANCZOS))
                small = ImageTk.PhotoImage(im.resize((32, 32), Image.LANCZOS))
        else:
            big = ImageTk.PhotoImage(glyph("cube", 64, ACCENT, BG))
            small = ImageTk.PhotoImage(glyph("cube", 32, ACCENT, BG))
        root.iconphoto(True, big, small)
        theme.images["icon"] = (big, small)
        ico = png.with_suffix(".ico")
        if WIN and ico.is_file():
            root.iconbitmap(default=str(ico))     # barra delle applicazioni e Alt+Tab
    except tk.TclError:
        pass


# ----------------------------------------------------------------- Windows
def double_buffer(root):
    """Windows compone tutta la finestra in memoria e la mostra in un colpo solo: mentre la
    si ridimensiona i pannelli non si ridisegnano più uno alla volta (niente scatti)."""
    if not WIN:
        return
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE, WS_EX_COMPOSITED = -20, 0x02000000
        u32.GetWindowLongW.restype = ctypes.c_long
        style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_COMPOSITED)
    except (OSError, AttributeError):
        pass


def app_id(name="MarcoLoBartolo.Effigie"):
    """Identità propria per Windows: senza, la finestra finisce raggruppata sotto
    l'icona di Python nella barra delle applicazioni. Va chiamata prima di creare Tk."""
    if WIN:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(name)
        except (OSError, AttributeError):
            pass


def dark_titlebar(root):
    """Barra del titolo scura su Windows 10/11 (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    if not WIN:
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.c_void_p(int(root.wm_frame(), 16))
        on = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi
        for attr in (20, 19):                     # 19 nelle versioni di Windows 10 più vecchie
            if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4) == 0:
                break
        color = ctypes.c_int(int(BG[5:7] + BG[3:5] + BG[1:3], 16))   # COLORREF 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)  # Windows 11: colore barra
    except (AttributeError, OSError, ValueError):
        pass


def flash_taskbar(root):
    """Fa lampeggiare il pulsante nella barra delle applicazioni finché non si torna qui."""
    if not WIN:
        return
    try:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                        ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                        ("dwTimeout", ctypes.c_uint)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), int(root.wm_frame(), 16), 0x3 | 0xC, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except (AttributeError, OSError, ValueError):
        pass


def enable_file_drop(root, callback):
    """Accetta file e cartelle trascinati da Esplora risorse (messaggio WM_DROPFILES).
    callback(lista di percorsi) viene chiamata nel thread dell'interfaccia."""
    if not WIN:
        return False
    try:
        import ctypes.wintypes as wt
        u32, shell = ctypes.windll.user32, ctypes.windll.shell32
        hwnd = int(root.wm_frame(), 16)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        u32.GetWindowLongPtrW.restype = ctypes.c_void_p
        u32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
        u32.SetWindowLongPtrW.restype = ctypes.c_void_p
        u32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        u32.CallWindowProcW.restype = LRESULT
        u32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        shell.DragQueryFileW.restype = ctypes.c_uint
        shell.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p,
                                         ctypes.c_uint]
        shell.DragFinish.argtypes = [ctypes.c_void_p]
        shell.DragAcceptFiles.argtypes = [wt.HWND, wt.BOOL]
        old = u32.GetWindowLongPtrW(hwnd, -4)                       # GWLP_WNDPROC

        def proc(h, msg, wparam, lparam):
            if msg == 0x0233:                                         # WM_DROPFILES
                n = shell.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                files = []
                for i in range(n):
                    size = shell.DragQueryFileW(wparam, i, None, 0) + 1
                    buf = ctypes.create_unicode_buffer(size)
                    shell.DragQueryFileW(wparam, i, buf, size)
                    files.append(buf.value)
                shell.DragFinish(wparam)
                root.after(0, callback, files)
                return 0
            return u32.CallWindowProcW(old, h, msg, wparam, lparam)
        root._drop_proc = WNDPROC(proc)                    # tenuto in vita con la finestra
        u32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(root._drop_proc, ctypes.c_void_p))
        shell.DragAcceptFiles(hwnd, True)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def number_badge(theme, n, bg, size=20):
    """Pallino colorato con il numero di una sezione."""
    key = f"badge:{n}:{bg}"
    if key not in theme.images:
        ss, S = 4, theme.px(size)
        img = Image.new("RGBA", (S * ss, S * ss), hex_rgba(bg))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, S * ss - 1, S * ss - 1), fill=hex_rgba(ACCENT))
        try:
            fnt = ImageFont.truetype("seguisb.ttf", int(S * ss * 0.55))
        except OSError:
            fnt = ImageFont.load_default()
        l, t, r, b = d.textbbox((0, 0), str(n), font=fnt)
        d.text(((S * ss - (l + r)) / 2, (S * ss - (t + b)) / 2), str(n), font=fnt,
               fill=(255, 255, 255, 255))
        theme.images[key] = ImageTk.PhotoImage(img.resize((S, S), Image.LANCZOS))
    return theme.images[key]
