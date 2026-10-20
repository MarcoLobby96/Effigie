#!/usr/bin/env python3
"""
screenshot_ui.py — Fotografa solo la finestra di Effigie in alcuni stati, per controllare la
grafica (PrintWindow: funziona anche se la finestra è coperta da altre).

  python scripts/screenshot_ui.py cartella_di_uscita [progetto_da_aprire]
"""
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from PIL import Image  # noqa: E402

import app  # noqa: E402


def grab(win, path):
    win.update()
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    r = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    gdi, user = ctypes.windll.gdi32, ctypes.windll.user32
    hdc = user.GetWindowDC(hwnd)
    mem = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    gdi.SelectObject(mem, bmp)
    user.PrintWindow(hwnd, mem, 2)                       # PW_RENDERFULLCONTENT
    buf = ctypes.create_string_buffer(w * h * 4)
    hdr = (ctypes.c_uint32 * 10)(40, w, -h, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
    gdi.GetDIBits(mem, bmp, 0, h, buf, hdr, 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    gdi.DeleteObject(bmp)
    gdi.DeleteDC(mem)
    user.ReleaseDC(hwnd, hdc)
    print("salvata", path)


def main():
    """Gli stati vengono preparati dentro il ciclo di Tk (come nell'uso reale: miniature e
    modello si caricano in thread che rispondono tramite il ciclo)."""
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    project = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    a = app.App()

    def processing():
        a.run_step = 1
        a.viewer.clear("Sto creando il modello…\nI dettagli sono nella scheda «Registro».")
        a.viewer.show_loader("Creo il modello", 0)
        a.handle_line("@@PROGRESS 14 features COLMAP: punti caratteristici")

    steps = [(500, lambda: grab(a, out / "1_vuota.png")),
             (100, processing), (300, lambda: grab(a, out / "2_elaborazione.png")),
             (100, a.viewer.hide_loader), (300, lambda: grab(a, out / "3_dopo_loader.png"))]
    if project:
        steps += [(100, lambda: a.open_project(project)),
                  (9000, lambda: grab(a, out / "4_modello.png")),
                  (100, lambda: (setattr(a, "run_step", 5),
                                 a.handle_line("@@PROGRESS 91 surface Superficie"))),
                  (300, lambda: grab(a, out / "5_rifinitura.png")),
                  (100, lambda: (a.viewer.hide_loader(), a.toggle_quad())),
                  (2500, lambda: grab(a, out / "6_quattro_viste.png"))]
    steps.append((200, a.destroy))

    def run(i=0):                    # un passaggio alla volta, ognuno dopo il precedente
        if i < len(steps):
            delay, fn = steps[i]
            a.after(delay, lambda: (fn(), run(i + 1)) if i < len(steps) - 1 else fn())
    run()
    a.mainloop()


if __name__ == "__main__":
    main()
