"""Ritocchi a mano sul modello, direttamente nell'app (senza Blender).

Pennelli dosati per non fare danni: ogni passata sposta i vertici di poco e solo dentro
il cerchio del pennello, con un bordo sfumato; tutto si annulla con Ctrl+Z.
- leviga: avvicina ogni vertice alla media dei vicini (via grana e bozzi)
- erodi: scava un poco verso l'interno e leviga, come carta vetrata
- base qui: la zona cliccata diventa il fondo piatto, appoggiato sul piatto
- ruota: rotazione libera, poi il modello torna appoggiato sul piatto
Il risultato si salva in scolpito.stl, che il visualizzatore e Bambu Studio preferiscono.
"""
from pathlib import Path

import numpy as np

UNDO = 30


def normals(V, F):
    v0, v1, v2 = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    N = np.zeros_like(V)
    for c in range(3):
        N[:, c] = np.bincount(F.ravel(), weights=np.repeat(fn[:, c], 3), minlength=len(V))
    N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-12
    return N.astype(np.float32)


class Sculptor:
    def __init__(self, V, F):
        self.V = np.ascontiguousarray(V, np.float32).copy()
        self.F = np.ascontiguousarray(F, np.uint32)
        self.N = normals(self.V, self.F)
        self.undo = []
        self._graph()
        self._outward()

    def _outward(self):
        """+1 se le normali guardano fuori, -1 se le facce sono girate al contrario
        (segno del volume): serve perché «erodi» scavi sempre verso l'interno."""
        v0, v1, v2 = (self.V[self.F[:, k]].astype(np.float64) for k in range(3))
        self.out = 1.0 if np.einsum("ij,ij->i", v0, np.cross(v1, v2)).sum() >= 0 else -1.0

    def _graph(self):
        """Vicini di ogni vertice (per levigare) e facce di ogni vertice (per le normali)."""
        from scipy.sparse import csr_matrix
        n, F = len(self.V), self.F.astype(np.int64)
        i = np.r_[F[:, 0], F[:, 1], F[:, 2], F[:, 1], F[:, 2], F[:, 0]]
        j = np.r_[F[:, 1], F[:, 2], F[:, 0], F[:, 0], F[:, 1], F[:, 2]]
        A = csr_matrix((np.ones(len(i), np.float32), (i, j)), shape=(n, n))
        A.data[:] = 1
        self.A = A
        self.deg = np.asarray(A.sum(1)).ravel().clip(1)
        f = np.repeat(np.arange(len(F)), 3)
        self.VF = csr_matrix((np.ones(len(f), np.float32), (F.ravel(), f)), shape=(n, len(F)))

    # ---------------------------------------------------------- stato
    def snapshot(self):
        self.undo.append((self.V.copy(), self.F.copy()))
        del self.undo[:-UNDO]

    def can_undo(self):
        return bool(self.undo)

    def undo_last(self):
        if not self.undo:
            return False
        V, F = self.undo.pop()
        topo = F.shape != self.F.shape or not np.array_equal(F, self.F)
        self.V, self.F = V, F
        if topo:
            self._graph()
            self._outward()
        self.N = normals(self.V, self.F)
        return True

    # ---------------------------------------------------------- scelta del punto
    def pick(self, a, b, tol):
        """Punto del modello sotto il raggio a→b (il più vicino a chi guarda), o None."""
        d = (b - a) / np.linalg.norm(b - a)
        rel = self.V - a
        t = rel @ d
        perp = np.linalg.norm(rel - np.outer(t, d), axis=1)
        ok = (perp < tol) & (t > 0) & (self.N @ d < 0.2)
        if not ok.any():
            return None
        k = np.nonzero(ok)[0][np.argmin(t[ok])]
        return self.V[k].astype(float)

    def region(self, c, radius):
        dist = np.linalg.norm(self.V - c, axis=1)
        idx = np.nonzero(dist < radius)[0]
        x = dist[idx] / radius
        return idx, (1 - x * x) ** 2                       # bordo sfumato

    # ---------------------------------------------------------- pennelli
    def brush(self, c, radius, strength, mode):
        """Una passata del pennello (strength 0-1). Ritorna gli indici toccati."""
        idx, w = self.region(c, radius)
        if not len(idx):
            return idx
        V = self.V.astype(np.float64)
        avg = (self.A[idx] @ V) / self.deg[idx, None]
        k = (0.5 * strength * w)[:, None]
        if mode == "erode":
            # scava al massimo l'1% del raggio per passata, tutta la zona nella stessa
            # direzione (la media delle normali: niente bolle sulle superfici ruvide), poi
            # leviga: come carta vetrata, mai buchi
            n = (self.N[idx].astype(np.float64) * w[:, None]).sum(0) * self.out
            n /= np.linalg.norm(n) + 1e-12
            V[idx] = V[idx] - np.outer(0.01 * radius * strength * w, n)
            avg = (self.A[idx] @ V) / self.deg[idx, None]
            k = (0.5 * strength * w)[:, None]
        V[idx] = V[idx] + (avg - V[idx]) * k
        self.V = V.astype(np.float32)
        self._local_normals(idx)
        return idx

    def _local_normals(self, idx):
        faces = np.unique(self.VF[idx].indices)
        F = self.F[faces]
        v0, v1, v2 = self.V[F[:, 0]], self.V[F[:, 1]], self.V[F[:, 2]]
        fn = np.cross(v1 - v0, v2 - v0)
        n = self.VF[idx][:, faces] @ fn
        self.N[idx] = n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)

    # ---------------------------------------------------------- base piatta
    def base_at(self, c, radius):
        """La zona attorno a c diventa il fondo: il modello si gira perché quella zona
        guardi in basso, poi si taglia con un piano poco sopra il punto più basso della
        zona, così il fondo è piatto e pieno. Ritorna False se non si può fare."""
        # direzione: media delle facce della zona pesate per area (stabile anche su una
        # faccia piatta fatta di pochi triangoli grandi, o cliccando vicino a uno spigolo)
        tri = self.V[self.F].astype(np.float64)
        cen = tri.mean(1)
        fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]) * self.out
        import trimesh
        hit = int(trimesh.proximity.closest_point(
            trimesh.Trimesh(self.V, self.F, process=False), [c])[2][0])
        up = fn[hit] / (np.linalg.norm(fn[hit]) + 1e-12)
        unit = fn / (np.linalg.norm(fn, axis=1, keepdims=True) + 1e-12)
        near = (np.linalg.norm(cen - c, axis=1) < radius) & (unit @ up > 0.3)
        near[hit] = True
        n = fn[near].sum(0)
        if np.linalg.norm(n) < 1e-12:
            return False
        n /= np.linalg.norm(n)
        P = tri[near].reshape(-1, 3)
        P = P[np.linalg.norm(P - c, axis=1) < radius] if (np.linalg.norm(P - c, axis=1)
                                                          < radius).sum() >= 3 else P
        m = np.asarray(c, float)
        R = rotation_between(n, np.array([0.0, 0.0, -1.0]))
        V = (self.V.astype(np.float64) - m) @ R.T + m
        # piano all'altezza della parte più interna della zona: tutta la zona diventa piatta
        h = (P - m) @ n                                     # quanto sporge verso fuori
        z_cut = m[2] - float(np.percentile(h, 5)) + 0.002 * float(np.ptp(self.V, 0).max())
        res = cut_below(V, self.F, z_cut)
        if res is None:
            return False
        self.snapshot()
        self.V, self.F = res
        self._drop()
        self._graph()
        self._outward()
        self.N = normals(self.V, self.F)
        return True

    # ---------------------------------------------------------- rotazione
    def rotate(self, axis, degrees):
        self.snapshot()
        a = np.radians(degrees)
        u = np.asarray(axis, float)
        R = axis_angle(u / np.linalg.norm(u), a)
        c = (self.V.min(0) + self.V.max(0)) / 2
        self.V = ((self.V.astype(np.float64) - c) @ R.T + c).astype(np.float32)
        self._drop()
        self.N = normals(self.V, self.F)

    def _drop(self):
        """Di nuovo appoggiato sul piatto e centrato."""
        lo, hi = self.V.min(0), self.V.max(0)
        self.V = (self.V - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]],
                                    np.float32)).astype(np.float32)

    # ---------------------------------------------------------- salvataggio
    def save(self, path):
        tri = self.V[self.F]
        v0, v1, v2 = tri[:, 0], tri[:, 1], tri[:, 2]
        fn = np.cross(v1 - v0, v2 - v0)
        fn /= np.linalg.norm(fn, axis=1, keepdims=True) + 1e-12
        rec = np.zeros(len(tri), np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)),
                                           ("a", "<u2")]))
        rec["n"], rec["v"] = fn, tri
        path = Path(path)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(b"Effigie scolpito".ljust(80, b" "))
            f.write(np.uint32(len(tri)).tobytes())
            f.write(rec.tobytes())
        tmp.replace(path)


def axis_angle(u, a):
    x, y, z = u
    c, s, C = np.cos(a), np.sin(a), 1 - np.cos(a)
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def rotation_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(a @ b)
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else axis_angle(np.array([1.0, 0, 0]), np.pi)
    return axis_angle(v / np.linalg.norm(v), np.arccos(np.clip(c, -1, 1)))


def cut_below(V, F, z):
    """Tiene la parte sopra il piano z, chiusa con un fondo piatto (manifold3d)."""
    try:
        import manifold3d as m3d
        man = m3d.Manifold(m3d.Mesh(vert_properties=np.asarray(V, np.float32),
                                    tri_verts=np.asarray(F, np.uint32)))
        if man.status() != m3d.Error.NoError:
            raise ValueError(man.status())
        out = man.trim_by_plane([0.0, 0.0, 1.0], float(z)).to_mesh()
        if not len(out.tri_verts):
            return None
        return (np.ascontiguousarray(out.vert_properties[:, :3], np.float32),
                np.ascontiguousarray(out.tri_verts, np.uint32))
    except Exception as e:                       # mesh non chiusa: taglio con trimesh
        print("Base con manifold3d non riuscita, uso trimesh:", e)
        import trimesh
        m = trimesh.Trimesh(V, F, process=False).slice_plane([0, 0, z], [0, 0, 1], cap=True)
        if m.is_empty:
            return None
        return (np.ascontiguousarray(m.vertices, np.float32),
                np.ascontiguousarray(m.faces, np.uint32))
