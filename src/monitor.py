"""Uso delle risorse del PC: CPU (anche per singolo core), memoria, GPU, memoria video e
disco. Su Windows legge i contatori di sistema (gli stessi di Gestione attività) senza
librerie esterne; altrove usa psutil, se c'è. La lettura avviene in un thread, una volta
al secondo: l'interfaccia legge solo l'ultimo valore e la storia recente."""
import ctypes
import os
import shutil
import sys
import threading
import time
from collections import defaultdict, deque

WIN = sys.platform == "win32"
HISTORY = 90                       # secondi di storia per i grafici


class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


class _PDHVAL(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_uint32), ("pad", ctypes.c_uint32),
                ("doubleValue", ctypes.c_double)]


class _PDHITEM(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PDHVAL)]


class _Pdh:
    """Contatori di prestazioni di Windows (pdh.dll) con istanze multiple (*)."""

    def __init__(self, paths):
        self.lib = ctypes.windll.pdh
        self.lib.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.query = ctypes.c_void_p()
        if self.lib.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQuery")
        self.counters = {}
        for key, path in paths.items():
            c = ctypes.c_void_p()
            if self.lib.PdhAddEnglishCounterW(self.query, path, None, ctypes.byref(c)) == 0:
                self.counters[key] = c
        self.lib.PdhCollectQueryData(self.query)

    def collect(self):
        self.lib.PdhCollectQueryData(self.query)

    def values(self, key):
        c = self.counters.get(key)
        if c is None:
            return []
        size, count = ctypes.c_uint32(0), ctypes.c_uint32(0)
        flags = 0x200 | 0x8000                    # double, senza limite a 100
        self.lib.PdhGetFormattedCounterArrayW(c, flags, ctypes.byref(size), ctypes.byref(count),
                                              None)
        if not size.value:
            return []
        buf = (ctypes.c_byte * size.value)()
        if self.lib.PdhGetFormattedCounterArrayW(c, flags, ctypes.byref(size),
                                                 ctypes.byref(count), buf):
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PDHITEM))
        return [(items[i].szName, items[i].FmtValue.doubleValue) for i in range(count.value)
                if items[i].FmtValue.CStatus in (0, 1)]


def _registry_hardware():
    """Nome della CPU, nome e memoria video della scheda grafica principale."""
    cpu, gpu, vram = None, None, None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(k, sub) as s:
                        size = int(winreg.QueryValueEx(s, "HardwareInformation.qwMemorySize")[0])
                        name = winreg.QueryValueEx(s, "DriverDesc")[0]
                except (OSError, ValueError, TypeError):
                    continue
                if vram is None or size > vram:          # la scheda con più memoria
                    gpu, vram = name, size
    except (ImportError, OSError):
        pass
    return cpu, gpu, vram


class Monitor:
    """Campiona le risorse una volta al secondo in un thread."""

    KEYS = ("cpu", "ram", "gpu", "vram")

    def __init__(self, disk_path=".", interval=1.0):
        self.disk_path = disk_path
        self.interval = interval
        self.history = {k: deque([None] * HISTORY, maxlen=HISTORY) for k in self.KEYS}
        self.last = {}
        self.cores = []
        self.cpu_name, self.gpu_name, self.vram_total = (None, None, None)
        self.n_threads = os.cpu_count() or 1
        self._prev = None
        self._pdh = None
        self._psutil = None
        if WIN:
            self.cpu_name, self.gpu_name, self.vram_total = _registry_hardware()
            try:
                self._pdh = _Pdh({"gpu": r"\GPU Engine(*)\Utilization Percentage",
                                  "vram": r"\GPU Adapter Memory(*)\Dedicated Usage",
                                  "core": r"\Processor(*)\% Processor Time"})
            except (OSError, AttributeError):
                self._pdh = None
        else:
            try:
                import psutil
                self._psutil = psutil
                psutil.cpu_percent(percpu=True)
            except ImportError:
                pass
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                s = self.sample()
            except Exception:                         # mai far cadere l'interfaccia
                s = {}
            self.last = s
            for k in self.KEYS:
                self.history[k].append(s.get(k))
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ letture
    def sample(self):
        s = {}
        if WIN:
            s["cpu"] = self._cpu_windows()
            mem = _MEMSTATUS()
            mem.dwLength = ctypes.sizeof(mem)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                s["ram_total"] = mem.ullTotalPhys
                s["ram_used"] = mem.ullTotalPhys - mem.ullAvailPhys
                s["ram"] = 100.0 * s["ram_used"] / max(mem.ullTotalPhys, 1)
            if self._pdh:
                self._pdh.collect()
                engines = defaultdict(float)
                for name, v in self._pdh.values("gpu"):
                    # stesso motore della stessa scheda: somma dei processi (come Gestione attività)
                    engines[name[name.find("luid"):]] += v
                if engines:
                    s["gpu"] = min(100.0, max(engines.values()))
                used = [v for _, v in self._pdh.values("vram")]
                if used:
                    s["vram_used"] = max(used)
                    if self.vram_total:
                        s["vram_total"] = self.vram_total
                        s["vram"] = 100.0 * s["vram_used"] / self.vram_total
                cores = sorted(((int(n), v) for n, v in self._pdh.values("core") if n.isdigit()))
                self.cores = [min(100.0, max(0.0, v)) for _, v in cores]
        elif self._psutil:
            ps = self._psutil
            self.cores = ps.cpu_percent(percpu=True)
            s["cpu"] = sum(self.cores) / max(len(self.cores), 1)
            vm = ps.virtual_memory()
            s.update(ram=vm.percent, ram_used=vm.total - vm.available, ram_total=vm.total)
        try:
            du = shutil.disk_usage(self.disk_path)
            s.update(disk_free=du.free, disk_total=du.total)
        except OSError:
            pass
        return s

    def _cpu_windows(self):
        idle, kern, user = _FILETIME(), _FILETIME(), _FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                              ctypes.byref(user))
        cur = tuple((t.hi << 32) | t.lo for t in (idle, kern, user))
        prev, self._prev = self._prev, cur
        if not prev:
            return None
        busy_total = (cur[1] - prev[1]) + (cur[2] - prev[2])     # kernel comprende l'idle
        if busy_total <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (cur[0] - prev[0]) / busy_total)))


def gb(n):
    return f"{n / 2 ** 30:.1f}"
