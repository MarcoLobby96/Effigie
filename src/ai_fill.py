"""
ai_fill.py — Riempie con l'IA solo le parti che le foto non hanno visto.

La fotogrammetria dà la forma vera dove le foto vedono; dove non vedono (sotto, dietro,
il lato contro una siepe) la superficie di Poisson inventa tappi piatti o "vele". Qui:
  1. TripoSR immagina la forma intera da una foto (ai_reconstruct.generate)
  2. la forma dell'IA viene allineata ai punti veri: per ognuna delle 24 rotazioni
     principali un ICP con scala, e si tiene quella che combacia meglio
  3. si aggiungono alla nuvola solo i punti dell'IA lontani da tutti i punti veri
Poisson poi costruisce una superficie unica: forma vera dove c'è, IA solo nei buchi.
"""
from pathlib import Path

import numpy as np


def _rotations():
    """Le 24 rotazioni che portano gli assi su assi (orientamenti di un cubo)."""
    mats = []
    for perm in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
        for signs in np.ndindex(2, 2, 2):
            m = np.zeros((3, 3))
            for i, (j, s) in enumerate(zip(perm, signs)):
                m[i, j] = -1 if s else 1
            if np.linalg.det(m) > 0:
                mats.append(m)
    return mats


def align(source, target, iterations=30):
    """Trasformazione 4x4 (rotazione, scala, traslazione) che porta source su target."""
    import trimesh
    from scipy.spatial import cKDTree
    tree = cKDTree(target)
    s0 = np.ptp(target, 0).max() / max(np.ptp(source, 0).max(), 1e-9)
    best = (np.inf, None)
    for R in _rotations():
        init = np.eye(4)
        init[:3, :3] = R * s0
        init[:3, 3] = target.mean(0) - (R * s0) @ source.mean(0)
        try:
            M, moved, cost = trimesh.registration.icp(source, target, initial=init,
                                                      threshold=1e-6, max_iterations=iterations,
                                                      reflection=False, scale=True)
        except Exception:
            continue
        # costo robusto: distanza mediana dai punti veri (gli outlier non contano)
        d = np.median(tree.query(moved)[0])
        if d < best[0]:
            best = (d, M)
    return best[1], best[0]


def fill_points(work, points, normals, photo, gap=0.03, progress=print):
    """Punti e normali dell'IA da aggiungere (solo dove i punti veri mancano)."""
    import trimesh
    from scipy.spatial import cKDTree
    import ai_reconstruct
    work = Path(work)
    mesh_file = work / "ia_riempimento.ply"
    if not mesh_file.is_file():
        ai_reconstruct.generate(photo, mesh_file, progress=progress)
    ai = trimesh.load(mesh_file, force="mesh")
    rng = np.random.default_rng(0)
    target = points[rng.choice(len(points), min(len(points), 40_000), replace=False)]
    src, fid = ai.sample(40_000, return_index=True)
    M, err = align(np.asarray(src), target)
    if M is None:
        return None
    diag = float(np.linalg.norm(np.ptp(points, 0)))
    progress(f"IA: forma allineata ai punti veri (scarto mediano {100 * err / diag:.1f}%).")
    if err > 0.03 * diag:
        progress("IA: la forma immaginata non combacia con le foto, non la uso.")
        return None
    dense, dfid = ai.sample(200_000, return_index=True)
    dense = trimesh.transform_points(dense, M)
    nrm = ai.face_normals[dfid] @ M[:3, :3].T
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    far = cKDTree(target).query(dense)[0] > gap * diag
    if far.sum() < 500:
        progress("IA: le foto coprono già tutto, niente da riempire.")
        return None
    progress(f"IA: riempio le parti non viste con {int(far.sum())} punti immaginati.")
    return dense[far], nrm[far]
