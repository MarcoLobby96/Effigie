#!/usr/bin/env python3
"""
Ricostruzione 3D "immaginata" da una sola foto con TripoSR (MIT, Stability AI + Tripo):
le parti non visibili vengono inventate dal modello. Gira su CPU (nessun CUDA).
Codice in vendor/tsr (TripoSR, licenza MIT, marching cubes con PyMCubes), pesi (~1.7 GB) scaricati da Hugging Face al primo uso.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent      # cartella del progetto (src/ è il codice)
sys.path.insert(0, str(ROOT / "vendor"))


def pick_photo(paths):
    """La foto più nitida e con il soggetto più grande (stima grezza: nitidezza)."""
    from bust_pipeline import sharpness
    return max(paths, key=sharpness)


def generate(photo, out_mesh, resolution=256, progress=print):
    import torch
    import rembg
    from PIL import Image
    from tsr.system import TSR
    from tsr.utils import remove_background, resize_foreground

    torch.set_num_threads(max(1, torch.get_num_threads()))
    progress("IA: carico il modello (la prima volta scarica circa 1.7 GB)...")
    model = TSR.from_pretrained("stabilityai/TripoSR", config_name="config.yaml",
                                weight_name="model.ckpt")
    model.renderer.set_chunk_size(8192)
    progress("IA: tolgo lo sfondo dalla foto...")
    image = remove_background(Image.open(photo).convert("RGB"), rembg.new_session())
    image = resize_foreground(image, 0.85)
    a = np.asarray(image).astype(np.float32) / 255.0
    a = a[:, :, :3] * a[:, :, 3:4] + (1 - a[:, :, 3:4]) * 0.5
    image = Image.fromarray((a * 255).astype(np.uint8))
    image.save(Path(out_mesh).with_name("ia_input.png"))
    progress("IA: immagino la forma 3D...")
    with torch.no_grad():
        codes = model([image], device="cpu")
    progress("IA: estraggo la superficie...")
    mesh = model.extract_mesh(codes, True, resolution=resolution)[0]
    mesh.export(out_mesh)
    return out_mesh


if __name__ == "__main__":
    generate(sys.argv[1], sys.argv[2])
