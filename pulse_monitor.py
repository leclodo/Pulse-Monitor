"""
Pulse Monitor - tableau de bord simple des performances et des composants du PC.
Pulse Monitor - simple dashboard for PC performance and components.

Lancement / Run :  python pulse_monitor.py   (ou double-clic sur "Lancer Pulse Monitor.bat")
Dépendances : psutil (obligatoire), pywebview (optionnel, pour une vraie fenêtre)
"""
import base64
import collections
import ctypes
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

APP_NAME = "Pulse Monitor"
APP_VERSION = "0.7.30"
IS_WIN = os.name == "nt"
FROZEN = bool(getattr(sys, "frozen", False))      # version .exe (PyInstaller)
# données (réglages, historique…) : à côté du .exe ou du script ; fichiers de l'appli (page, icônes) : dans le paquet
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
# données (réglages, historique, sessions…) : à côté du programme (script ou .exe portable),
# ou dans %LOCALAPPDATA%\Pulse Monitor pour la version installée (on ne peut pas écrire dans Program Files)
if FROZEN and not os.path.exists(os.path.join(BASE_DIR, "portable.txt")):
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Pulse Monitor")
else:
    DATA_DIR = BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
WEB_DIR = os.path.join(RES_DIR, "web")
ICON_ICO = os.path.join(RES_DIR, "assets", "icon.ico")
ICON_PNG = os.path.join(RES_DIR, "assets", "icon.png")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
SCANS_DIR = os.path.join(DATA_DIR, "scans")
HIST_DIR = os.path.join(DATA_DIR, "history")
HEALTH_FILE = os.path.join(DATA_DIR, "scans", "health.json")
GAMES_FILE = os.path.join(DATA_DIR, "games.json")
GAMELIB_FILE = os.path.join(DATA_DIR, "games_lib.json")

LOG_FILE = os.path.join(DATA_DIR, "pulse_monitor.log")
RUN_FLAG = os.path.join(DATA_DIR, "running.flag")      # présent pendant que l'appli tourne : sert à repérer un plantage


def _fix_log_encoding():
    """Le journal est en UTF-8 (avec la marque BOM pour que le Bloc-notes affiche bien les accents).
    Les anciennes lignes écrites dans l'encodage Windows sont converties une fois."""
    try:
        with open(LOG_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        raw = None
    if raw is None or raw == b"":
        try:
            with open(LOG_FILE, "wb") as f:
                f.write(b"\xef\xbb\xbf")
        except OSError:
            pass
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        return
    out = []
    for line in raw.splitlines(keepends=True):
        try:
            out.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(line.decode("cp1252", errors="replace"))
    try:
        with open(LOG_FILE, "w", encoding="utf-8-sig", newline="") as f:
            f.write("".join(out))
    except OSError:
        pass


def _setup_log():
    """Journal limité à 1 Mo (+ 1 ancienne copie). La fenêtre (2e processus) écrit sans faire tourner le fichier."""
    if "--window" not in sys.argv:
        _fix_log_encoding()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if "--window" in sys.argv:
        h = logging.FileHandler(LOG_FILE, encoding="utf-8")
    else:
        from logging.handlers import RotatingFileHandler

        class Rot(RotatingFileHandler):
            def doRollover(self):                 # nouveau fichier : on remet la marque UTF-8 en tête
                super().doRollover()
                if self.stream:
                    self.stream.write("\ufeff")
        h = Rot(LOG_FILE, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
    h.setFormatter(fmt)
    lg = logging.getLogger()
    lg.setLevel(logging.INFO)
    lg.addHandler(h)

    # erreurs non prévues (plantages) : dans le journal, avec leur détail
    def hook(t, v, tb):
        logging.getLogger("pulse").critical("Crash (%s)", "window" if "--window" in sys.argv else "app", exc_info=(t, v, tb))
    sys.excepthook = hook
    threading.excepthook = lambda a: logging.getLogger("pulse").critical(
        "Crash in thread (%s)", getattr(a.thread, "name", "?"), exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    # plantage « dur » (ex. dans une fonction Windows) : trace écrite directement dans le journal
    try:
        import faulthandler
        _setup_log.crash_file = open(LOG_FILE + ".crash", "a", encoding="utf-8")
        faulthandler.enable(_setup_log.crash_file)
    except Exception:
        pass


_setup_log()
log = logging.getLogger("pulse")

CREATE_NO_WINDOW = 0x08000000
GB = 1024 ** 3

# Dernière requête de l'interface : quand plus personne ne regarde, on arrête de mesurer.
LAST_PING = [time.time()]
SHOW_HOOK = [None]   # ouvre la fenêtre (appelé par une 2e instance)
BG = [True]          # analyse d'usage en arrière-plan (réglage « bg »)
WIDGET_ON = [False]  # widget de bureau affiché
WIDGET_HOOK = [None] # applique les réglages du widget (appelé quand Paramètres change)
HIST_ON = [True]     # enregistrement de l'historique (réglage « history »)
GAMES_ON = [True]    # résumé des sessions de jeu (réglage « games »)
GAMES = [None]       # GameTracker
QUIT_HOOK = [None]   # ferme proprement l'appli (utilisé après une mise à jour)
# Dépôt GitHub des mises à jour (« propriétaire/nom »). Seule connexion Internet de l'appli, et seulement à ta demande.
UPDATE_REPO = "leclodo/Pulse-Monitor"
IDLE_AFTER = 6.0


def ui_idle():
    return time.time() - LAST_PING[0] > IDLE_AFTER


# --------------------------------------------------------------------------
# PowerShell (lecture unique des composants au démarrage)
# --------------------------------------------------------------------------
def _ps_args(script):
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encoded]


def run_ps_json(script, timeout=90):
    try:
        out = subprocess.run(_ps_args(script), capture_output=True, timeout=timeout,
                             creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
        text = out.stdout.decode("utf-8", errors="replace").strip()
        return json.loads(text) if text else None
    except Exception:
        log.exception("PowerShell failed")
        return None


SPECS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$st = 'root/Microsoft/Windows/Storage'
$o = [ordered]@{}
$o.cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,SocketDesignation,L2CacheSize,L3CacheSize)
$o.ram = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,DeviceLocator,BankLabel,SMBIOSMemoryType,FormFactor)
$o.ramArray = @(Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MaxCapacity,MaxCapacityEx,MemoryDevices,Use)
$o.gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,DriverVersion,@{n='DriverDate';e={ if ($_.DriverDate) { $_.DriverDate.ToString('yyyy-MM-dd') } }},CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate,PNPDeviceID)
$o.gpuReg = @(Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' | ForEach-Object {
    $p = Get-ItemProperty $_.PSPath
    if ($p.DriverDesc) { [pscustomobject]@{ Name = $p.DriverDesc; Mem = $p.'HardwareInformation.qwMemorySize' } }
})
$o.board = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,Version
$o.bios = Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,@{n='ReleaseDate';e={ if ($_.ReleaseDate) { $_.ReleaseDate.ToString('yyyy-MM-dd') } }}
$o.os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture
$o.system = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory
$o.disks = @(Get-CimInstance -Namespace $st -ClassName MSFT_PhysicalDisk | Select-Object DeviceId,FriendlyName,MediaType,BusType,Size,HealthStatus)
$o.parts = @(Get-CimInstance -Namespace $st -ClassName MSFT_Partition | ForEach-Object {
    $l = [int][char]$_.DriveLetter
    if ($l -gt 0) { [pscustomobject]@{ DiskNumber = $_.DiskNumber; Letter = [string][char]$l } }
})
$o.net = @(Get-CimInstance Win32_NetworkAdapter -Filter "PhysicalAdapter=True" | Where-Object { $_.NetEnabled } | Select-Object Name,NetConnectionID,Speed)
$o.monitors = @(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorID | ForEach-Object {
    [pscustomobject]@{
        Maker = (-join ($_.ManufacturerName | Where-Object { $_ } | ForEach-Object { [char]$_ }))
        Name  = (-join ($_.UserFriendlyName | Where-Object { $_ } | ForEach-Object { [char]$_ }))
    }
})
$o | ConvertTo-Json -Depth 4 -Compress
"""

# Plan B si l'API PDH ne marche pas : compteurs bruts via PowerShell (plus gourmand).
COUNTERS_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
while ($true) {
    $e = Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUEngine
    $m = Get-CimInstance Win32_PerfRawData_GPUPerformanceCounters_GPUAdapterMemory
    $c = Get-CimInstance Win32_PerfRawData_Counters_ProcessorInformation -Filter "Name='_Total'"
    $obj = @{
        e = @($e | ForEach-Object { "$($_.Name)|$($_.UtilizationPercentage)|$($_.Timestamp_Sys100NS)" })
        m = @($m | ForEach-Object { "$($_.Name)|$($_.DedicatedUsage)|$($_.SharedUsage)" })
        c = "$($c.PercentProcessorPerformance)|$($c.PercentProcessorPerformance_Base)"
    }
    [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress))
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 1500
}
"""


# --------------------------------------------------------------------------
# GPU : noms et températures (D3DKMT, comme le Gestionnaire des tâches)
# --------------------------------------------------------------------------
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32), ("AdapterLuid", _LUID),
                ("NumOfSources", ctypes.c_uint32), ("bPrecise", ctypes.c_int32)]


class _ENUMADAPTERS2(ctypes.Structure):
    _fields_ = [("NumAdapters", ctypes.c_uint32), ("pAdapters", ctypes.POINTER(_ADAPTERINFO))]


class _QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint32), ("Type", ctypes.c_int32),
                ("pPrivateDriverData", ctypes.c_void_p), ("PrivateDriverDataSize", ctypes.c_uint32)]


class _REGISTRYINFO(ctypes.Structure):
    _fields_ = [("AdapterString", ctypes.c_wchar * 260), ("BiosString", ctypes.c_wchar * 260),
                ("DacType", ctypes.c_wchar * 260), ("ChipType", ctypes.c_wchar * 260)]


class _PERFDATA(ctypes.Structure):
    _fields_ = [("PhysicalAdapterIndex", ctypes.c_uint32),
                ("MemoryFrequency", ctypes.c_uint64), ("MaxMemoryFrequency", ctypes.c_uint64),
                ("MaxMemoryFrequencyOC", ctypes.c_uint64), ("MemoryBandwidth", ctypes.c_uint64),
                ("PCIEBandwidth", ctypes.c_uint64), ("FanRPM", ctypes.c_uint32),
                ("Power", ctypes.c_uint32), ("Temperature", ctypes.c_uint32),
                ("PowerStateOverride", ctypes.c_ubyte)]


KMTQAITYPE_ADAPTERREGISTRYINFO = 8
KMTQAITYPE_ADAPTERPERFDATA = 62


class D3DKMT:
    def __init__(self):
        self.adapters = []  # [{luid, name, handle}]
        if not IS_WIN:
            return
        try:
            gdi = ctypes.WinDLL("gdi32")
            self._query = gdi.D3DKMTQueryAdapterInfo
            enum = gdi.D3DKMTEnumAdapters2
            req = _ENUMADAPTERS2(0, None)
            enum(ctypes.byref(req))
            arr = (_ADAPTERINFO * req.NumAdapters)()
            req.pAdapters = ctypes.cast(arr, ctypes.POINTER(_ADAPTERINFO))
            if enum(ctypes.byref(req)) != 0:
                return
            seen = set()
            for i in range(req.NumAdapters):
                a = arr[i]
                luid = "luid_0x%08x_0x%08x" % (a.AdapterLuid.HighPart & 0xFFFFFFFF, a.AdapterLuid.LowPart)
                info = _REGISTRYINFO()
                name = ""
                if self._q(a.hAdapter, KMTQAITYPE_ADAPTERREGISTRYINFO, info):
                    name = info.AdapterString.strip()
                if not name or "basic render" in name.lower() or luid in seen:
                    continue
                seen.add(luid)
                self.adapters.append({"luid": luid, "name": name, "handle": a.hAdapter})
            log.info("GPU (D3DKMT) : %s", [(a["name"], a["luid"]) for a in self.adapters])
        except Exception:
            log.exception("D3DKMT unavailable")
            self.adapters = []

    def _q(self, handle, qtype, struct):
        q = _QUERYADAPTERINFO(handle, qtype, ctypes.cast(ctypes.byref(struct), ctypes.c_void_p),
                              ctypes.sizeof(struct))
        return self._query(ctypes.byref(q)) == 0

    def temperature(self, handle):
        try:
            p = _PERFDATA()
            if self._q(handle, KMTQAITYPE_ADAPTERPERFDATA, p) and p.Temperature:
                t = p.Temperature / 10.0
                return t if 0 < t < 130 else None
        except Exception:
            pass
        return None


# --------------------------------------------------------------------------
# Compteurs de performance Windows (PDH) : GPU + fréquence CPU réelle
# --------------------------------------------------------------------------
class _PDH_VALUE(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_uint32), ("doubleValue", ctypes.c_double)]


class _PDH_ITEM(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("value", _PDH_VALUE)]


PDH_FMT = 0x00000200 | 0x00008000  # DOUBLE | NOCAP100
PDH_MORE_DATA = 0x800007D2


class PdhCounters:
    """API native de Windows : légère, et indépendante de la langue (noms anglais)."""
    PATHS = {
        "eng": r"\GPU Engine(*)\Utilization Percentage",
        "ded": r"\GPU Adapter Memory(*)\Dedicated Usage",
        "sh": r"\GPU Adapter Memory(*)\Shared Usage",
        "perf": r"\Processor Information(_Total)\% Processor Performance",
    }

    def __init__(self):
        p = ctypes.WinDLL("pdh")
        u32, vp = ctypes.c_uint32, ctypes.c_void_p
        p.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(vp)]
        p.PdhAddEnglishCounterW.argtypes = [vp, ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(vp)]
        p.PdhCollectQueryData.argtypes = [vp]
        p.PdhGetFormattedCounterArrayW.argtypes = [vp, u32, ctypes.POINTER(u32), ctypes.POINTER(u32), vp]
        p.PdhGetFormattedCounterValue.argtypes = [vp, u32, ctypes.POINTER(u32), ctypes.POINTER(_PDH_VALUE)]
        for f in (p.PdhOpenQueryW, p.PdhAddEnglishCounterW, p.PdhCollectQueryData,
                  p.PdhGetFormattedCounterArrayW, p.PdhGetFormattedCounterValue):
            f.restype = u32
        self.p = p
        self.q = vp()
        if p.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
            raise OSError("PdhOpenQuery")
        self.h = {}
        for k, path in self.PATHS.items():
            h = vp()
            if p.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0:
                self.h[k] = h
        if not self.h:
            raise OSError("aucun compteur PDH")
        p.PdhCollectQueryData(self.q)
        self._buf = None

    def _array(self, h):
        size, count = ctypes.c_uint32(0), ctypes.c_uint32(0)
        r = self.p.PdhGetFormattedCounterArrayW(h, PDH_FMT, ctypes.byref(size), ctypes.byref(count), None)
        if r != PDH_MORE_DATA:
            return {}
        if self._buf is None or len(self._buf) < size.value:
            self._buf = (ctypes.c_byte * (size.value + 4096))()
        size.value = len(self._buf)
        if self.p.PdhGetFormattedCounterArrayW(h, PDH_FMT, ctypes.byref(size), ctypes.byref(count),
                                               ctypes.cast(self._buf, ctypes.c_void_p)) != 0:
            return {}
        items = ctypes.cast(self._buf, ctypes.POINTER(_PDH_ITEM))
        out = {}
        for i in range(count.value):
            it = items[i]
            if it.value.CStatus in (0, 1) and it.szName:
                out[it.szName] = out.get(it.szName, 0.0) + it.value.doubleValue
        return out

    def process_memory(self):
        """Requête séparée, collectée seulement quand le détail GPU est ouvert."""
        if not hasattr(self, "_pq"):
            self._pq = ctypes.c_void_p()
            self._ph = ctypes.c_void_p()
            if self.p.PdhOpenQueryW(None, 0, ctypes.byref(self._pq)) != 0 or \
                    self.p.PdhAddEnglishCounterW(self._pq, r"\GPU Process Memory(*)\Dedicated Usage", 0,
                                                 ctypes.byref(self._ph)) != 0:
                self._ph = None
        if not self._ph or self.p.PdhCollectQueryData(self._pq) != 0:
            return {}
        return self._array(self._ph)

    def read(self):
        """-> (engines {nom: %}, memory {nom: (dédiée, partagée)}, perf_cpu %)"""
        if self.p.PdhCollectQueryData(self.q) != 0:
            return {}, {}, None
        eng = self._array(self.h["eng"]) if "eng" in self.h else {}
        ded = self._array(self.h["ded"]) if "ded" in self.h else {}
        sh = self._array(self.h["sh"]) if "sh" in self.h else {}
        mem = {k: (ded.get(k, 0), sh.get(k, 0)) for k in set(ded) | set(sh)}
        perf = None
        if "perf" in self.h:
            v = _PDH_VALUE()
            if self.p.PdhGetFormattedCounterValue(self.h["perf"], PDH_FMT, None, ctypes.byref(v)) == 0 \
                    and v.CStatus in (0, 1):
                perf = v.doubleValue
        return eng, mem, perf


def _luid_of(name):
    low = name.lower()
    try:
        return low[low.index("luid_"):low.index("_phys")]
    except ValueError:
        return low


def aggregate_gpu(engines):
    """Comme le Gestionnaire des tâches : somme par type de moteur, puis le max."""
    per = {}
    for name, pct in engines.items():
        low = name.lower()
        if "engtype_" not in low:
            continue
        key = (_luid_of(low), low.split("engtype_", 1)[1])
        per[key] = per.get(key, 0.0) + max(0.0, pct)
    usage = {}
    for (luid, _eng), v in per.items():
        usage[luid] = min(100.0, max(usage.get(luid, 0.0), v))
    return usage


def aggregate_mem(mem):
    out = {}
    for name, (d, s) in mem.items():
        luid = _luid_of(name)
        a, b = out.get(luid, (0, 0))
        out[luid] = (a + int(d or 0), b + int(s or 0))
    return out


class CounterReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.gpu_usage, self.gpu_mem, self.cpu_perf = {}, {}, None
        self.engines = {}   # moteurs GPU bruts (par processus), pour le détail
        self.pdh = None
        self.proc = None
        self._prev_e, self._prev_c = {}, None

    def snapshot(self):
        with self.lock:
            return dict(self.gpu_usage), dict(self.gpu_mem), self.cpu_perf

    def gpu_by_pid(self, luid):
        """{pid: % d'utilisation} pour une carte graphique (même calcul que le Gestionnaire des tâches)."""
        with self.lock:
            engines = dict(self.engines)
        per = {}
        for name, pct in engines.items():
            low = name.lower()
            if luid and luid not in low:
                continue
            try:
                pid = int(low.split("pid_", 1)[1].split("_", 1)[0])
                eng = low.split("engtype_", 1)[1]
            except (IndexError, ValueError):
                continue
            per[(pid, eng)] = per.get((pid, eng), 0.0) + max(0.0, pct)
        out = {}
        for (pid, _e), v in per.items():
            out[pid] = min(100.0, max(out.get(pid, 0.0), v))
        return out

    def vram_by_pid(self, luid):
        """{pid: octets de mémoire vidéo dédiée} - lu uniquement à la demande."""
        if not self.pdh:
            return {}
        try:
            mem = self.pdh.process_memory()
        except Exception:
            log.exception("GPU memory per process")
            return {}
        out = {}
        for name, v in mem.items():
            low = name.lower()
            if luid and luid not in low:
                continue
            try:
                pid = int(low.split("pid_", 1)[1].split("_", 1)[0])
            except (IndexError, ValueError):
                continue
            out[pid] = out.get(pid, 0) + int(v or 0)
        return out

    def _store(self, usage, mem, perf, engines=None):
        with self.lock:
            if engines is not None:
                self.engines = engines
            self.gpu_usage, self.gpu_mem = usage, mem
            if perf is not None and 0 < perf < 400:
                self.cpu_perf = perf

    def run(self):
        if not IS_WIN:
            return
        try:
            pdh = PdhCounters()
            self.pdh = pdh
            log.info("PDH counters active: %s", list(pdh.h))
        except Exception:
            log.exception("PDH unavailable, falling back to PowerShell")
            return self._run_powershell()
        n = 0
        while True:
            time.sleep(1)
            n += 1
            if ui_idle():
                # fenêtre fermée : toutes les 2 s pour l'analyse ou le widget, toutes les 5 s pour l'historique / les jeux, sinon rien
                if BG[0] or WIDGET_ON[0]:
                    if n % 2:
                        continue
                elif HIST_ON[0] or GAMES_ON[0]:
                    if n % 5:
                        continue
                else:
                    continue
            try:
                eng, mem, perf = pdh.read()
                self._store(aggregate_gpu(eng), aggregate_mem(mem), perf, eng)
            except Exception:
                log.exception("PDH read")
                time.sleep(5)

    # ---- plan B ----
    def _run_powershell(self):
        while True:
            try:
                self.proc = subprocess.Popen(_ps_args(COUNTERS_PS), stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                             creationflags=CREATE_NO_WINDOW)
                for raw in self.proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith("{"):
                        self._parse_ps(json.loads(line))
            except Exception:
                log.exception("Counter reading stopped")
            time.sleep(5)

    def _parse_ps(self, d):
        engines, new_prev = {}, {}
        for item in d.get("e") or []:
            try:
                name, val, ts = item.rsplit("|", 2)
                val, ts = int(val), int(ts)
            except ValueError:
                continue
            new_prev[name] = (val, ts)
            old = self._prev_e.get(name)
            if old and ts > old[1]:
                engines[name] = (val - old[0]) / (ts - old[1]) * 100.0
        self._prev_e = new_prev
        mem = {}
        for item in d.get("m") or []:
            try:
                name, ded, sh = item.rsplit("|", 2)
                mem[name] = (int(ded or 0), int(sh or 0))
            except ValueError:
                continue
        perf = None
        try:
            v, b = (int(x) for x in str(d.get("c") or "").split("|"))
            if self._prev_c and b > self._prev_c[1]:
                perf = (v - self._prev_c[0]) / (b - self._prev_c[1])
            self._prev_c = (v, b)
        except ValueError:
            pass
        self._store(aggregate_gpu(engines), aggregate_mem(mem), perf, engines)


# --------------------------------------------------------------------------
# Consommation de l'appli elle-même (Python + fenêtre + sous-processus)
# --------------------------------------------------------------------------
class SelfUsage:
    def __init__(self):
        self.me = psutil.Process()
        self.procs = {}
        self.t_list = 0.0
        self.ncpu = psutil.cpu_count() or 1
        self.window_pid = lambda: None
        self.ram_cache = {}   # pid -> mémoire (relue toutes les 5 s, c'est un peu plus coûteux)
        self.t_ram = 0.0

    def _ram(self, p, refresh):
        if refresh or p.pid not in self.ram_cache:
            try:
                v = p.memory_full_info().uss   # = « mémoire » du Gestionnaire des tâches (jeu de travail privé)
            except (psutil.Error, AttributeError):
                m = p.memory_info()
                v = getattr(m, "private", 0) or m.rss
            self.ram_cache[p.pid] = v
        return self.ram_cache[p.pid]

    def sample(self):
        now = time.time()
        refresh = now - self.t_ram > 5
        if refresh:
            self.t_ram = now
        if now - self.t_list > 5:
            self.t_list = now
            try:
                kids = self.me.children(recursive=True)
            except psutil.Error:
                kids = []
            cur = {self.me.pid: self.me}
            cur.update({k.pid: k for k in kids})
            for pid, p in cur.items():
                if pid not in self.procs:
                    try:
                        p.cpu_percent(None)
                        self.procs[pid] = p
                    except psutil.Error:
                        pass
            for pid in list(self.procs):
                if pid not in cur:
                    del self.procs[pid]
                    self.ram_cache.pop(pid, None)
        total_cpu, total_ram, parts = 0.0, 0, {}
        for pid, p in list(self.procs.items()):
            try:
                c = p.cpu_percent(None) / self.ncpu
                ram = self._ram(p, refresh)
                name = p.name().lower()
            except psutil.Error:
                self.procs.pop(pid, None)
                continue
            kind = ("window" if pid == self.window_pid() or "webview" in name or "edge" in name or "chrome" in name or "brave" in name
                    else "powershell" if "powershell" in name else "python" if "py" in name else "other")
            a = parts.setdefault(kind, [0.0, 0, 0])
            a[0] += c
            a[1] += ram
            a[2] += 1
            total_cpu += c
            total_ram += ram
        return {"cpu": total_cpu, "ram": total_ram,
                "parts": [{"kind": k, "cpu": v[0], "ram": v[1], "n": v[2]} for k, v in parts.items()]}


# --------------------------------------------------------------------------
# Rendre la RAM à Windows quand la fenêtre est réduite
# --------------------------------------------------------------------------
def trim_memory():
    """Vide le « jeu de travail » de l'appli et de sa fenêtre : Windows récupère la RAM inutilisée.
    Elle revient progressivement (et seulement ce qui sert) quand on rouvre la fenêtre."""
    import gc
    gc.collect()
    if not IS_WIN:
        return 0
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    try:
        pids = [os.getpid()] + [c.pid for c in psutil.Process().children(recursive=True)]
    except psutil.Error:
        pids = [os.getpid()]
    done = 0
    for pid in pids:
        h = k32.OpenProcess(0x0100 | 0x0400 | 0x1000, False, pid)  # SET_QUOTA | QUERY_INFO | QUERY_LIMITED
        if h:
            done += bool(psapi.EmptyWorkingSet(h))
            k32.CloseHandle(h)
    return done


# Fenêtre WebView2 au régime : pas d'accélération GPU (inutile pour des petits graphiques),
# un seul processus d'affichage, pas de services réseau / mises à jour en arrière-plan.
LEAN_BROWSER_ARGS = [
    "--disable-gpu", "--disable-gpu-compositing", "--in-process-gpu", "--disable-software-rasterizer",
    "--renderer-process-limit=1", "--process-per-site", "--disable-extensions",
    "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--disable-features=Translate,OptimizationHints,MediaRouter,SpareRendererForSitePerProcess,"
    "msSmartScreenProtection,AutofillServerCommunication,CalculateNativeWinOcclusion",
    "--js-flags=--max-old-space-size=96",
]


# --------------------------------------------------------------------------
# Détail par programme (calculé seulement quand un panneau « Détails » est ouvert)
# --------------------------------------------------------------------------
class ProcDetails:
    def __init__(self, counters):
        self.counters = counters
        self.lock = threading.Lock()
        self.last = None   # (temps, {pid: (nom, cpu_s, ram, lus, écrits)})
        self.ncpu = psutil.cpu_count() or 1

    @staticmethod
    def _snap():
        out = {}
        for p in psutil.process_iter(["name"]):
            pid = p.pid
            if pid == 0:
                continue
            name = p.info.get("name") or "?"
            cpu = ram = rd = wr = None
            try:
                with p.oneshot():
                    try:
                        t = p.cpu_times()
                        cpu = t.user + t.system
                    except psutil.Error:
                        pass
                    try:
                        m = p.memory_info()
                        ram = getattr(m, "private", 0) or m.rss
                    except psutil.Error:
                        pass
                    try:
                        io = p.io_counters()
                        rd, wr = io.read_bytes, io.write_bytes
                    except (psutil.Error, AttributeError, NotImplementedError):
                        pass
            except psutil.Error:
                continue
            out[pid] = (name, cpu, ram, rd, wr)
        return time.time(), out

    def compute(self, kind, luid=None, top=12):
        with self.lock:
            now, cur = self._snap()
            prev = self.last
            if prev is None or now - prev[0] > 15:      # 1re ouverture : 2e mesure rapide pour avoir des vitesses
                prev = (now, cur)
                time.sleep(0.6)
                now, cur = self._snap()
            self.last = (now, cur)
        dt = max(0.2, now - prev[0])
        gpu = self.counters.gpu_by_pid(luid) if kind == "gpu" else {}
        vram = self.counters.vram_by_pid(luid) if kind == "gpu" else {}
        groups = {}
        for pid, (name, cpu, ram, rd, wr) in cur.items():
            old = prev[1].get(pid)
            g = groups.setdefault(name.lower(), {"name": name[:-4] if name.lower().endswith(".exe") else name,
                                                 "n": 0, "cpu": 0.0, "ram": 0, "read": 0.0, "write": 0.0,
                                                 "gpu": 0.0, "vram": 0})
            g["n"] += 1
            g["ram"] += ram or 0
            if old and old[1] is not None and cpu is not None:
                g["cpu"] += max(0.0, (cpu - old[1]) / dt / self.ncpu * 100)
            if old and old[3] is not None and rd is not None:
                g["read"] += max(0.0, (rd - old[3]) / dt)
                g["write"] += max(0.0, (wr - old[4]) / dt)
            g["gpu"] += gpu.get(pid, 0.0)
            g["vram"] += vram.get(pid, 0)
        items = list(groups.values())
        for g in items:
            g["gpu"] = min(100.0, g["gpu"])
        key = {"cpu": lambda g: g["cpu"], "ram": lambda g: g["ram"],
               "disk": lambda g: g["read"] + g["write"],
               "gpu": lambda g: (g["gpu"], g["vram"])}.get(kind, lambda g: g["cpu"])
        items.sort(key=key, reverse=True)
        return {"kind": kind, "items": items[:top], "procs": len(cur), "groups": len(items),
                "vram_ok": bool(vram)}


# --------------------------------------------------------------------------
# Composants (specs) + conseils d'upgrade (FR / EN)
# --------------------------------------------------------------------------
RAM_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2",
             29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
RAM_FORMS = {8: "DIMM", 12: "SO-DIMM"}
MEDIA = {3: "HDD", 4: "SSD", 5: "SCM"}
BUS = {1: "SCSI", 3: "ATA", 7: "USB", 8: "RAID", 10: "SAS", 11: "SATA", 12: "SD", 17: "NVMe"}


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _days_since(iso):
    try:
        return (date.today() - date.fromisoformat(str(iso)[:10])).days
    except Exception:
        return None


def collect_specs(kmt):
    raw = (run_ps_json(SPECS_PS) if IS_WIN else None) or {}
    s = {"cpu": {}, "ram": {}, "gpus": [], "board": {}, "disks": [], "os": {}, "net": [],
         "monitors": [], "tips": [], "parts": {}}

    cpus = _as_list(raw.get("cpu"))
    c = cpus[0] if cpus else {}
    f = psutil.cpu_freq()
    s["cpu"] = {
        "name": (c.get("Name") or platform.processor() or "CPU").strip(),
        "cores": c.get("NumberOfCores") or psutil.cpu_count(logical=False),
        "threads": c.get("NumberOfLogicalProcessors") or psutil.cpu_count(),
        "base_mhz": c.get("MaxClockSpeed") or (f.max if f and f.max else None),
        "socket": c.get("SocketDesignation"),
        "l2_kb": c.get("L2CacheSize"),
        "l3_kb": c.get("L3CacheSize"),
    }

    mods = []
    for m in _as_list(raw.get("ram")):
        mods.append({
            "slot": m.get("DeviceLocator") or m.get("BankLabel") or "?",
            "size_gb": round((m.get("Capacity") or 0) / GB, 1),
            "speed": m.get("Speed"),
            "configured": m.get("ConfiguredClockSpeed"),
            "maker": (m.get("Manufacturer") or "").strip(),
            "part": (m.get("PartNumber") or "").strip(),
            "type": RAM_TYPES.get(m.get("SMBIOSMemoryType")),
            "form": RAM_FORMS.get(m.get("FormFactor")),
        })
    arrays = [a for a in _as_list(raw.get("ramArray")) if a.get("Use") in (3, None)] or _as_list(raw.get("ramArray"))
    slots = sum(a.get("MemoryDevices") or 0 for a in arrays) or None
    max_kb = sum(max(a.get("MaxCapacity") or 0, a.get("MaxCapacityEx") or 0) for a in arrays)
    s["ram"] = {
        "total_gb": round(sum(m["size_gb"] for m in mods), 1) or round(psutil.virtual_memory().total / GB, 1),
        "modules": mods,
        "slots": slots,
        "slots_used": len(mods) if mods else None,
        "max_gb": round(max_kb / 1024 / 1024) if max_kb else None,
        "type": next((m["type"] for m in mods if m["type"]), None),
        "form": next((m["form"] for m in mods if m["form"]), None),
        "speed": max((m["speed"] or 0) for m in mods) if mods else None,
        "configured": min((m["configured"] or 0) for m in mods) if mods else None,
    }

    reg = {}
    for r in _as_list(raw.get("gpuReg")):
        if r.get("Name") and r.get("Mem"):
            n = r["Name"].strip()
            reg[n] = max(reg.get(n, 0), int(r["Mem"]))
    for g in _as_list(raw.get("gpu")):
        name = (g.get("Name") or "").strip()
        low = name.lower()
        if not name or any(k in low for k in ("basic", "remote", "virtual", "parsec", "meta")):
            continue
        vram = reg.get(name) or g.get("AdapterRAM") or 0
        s["gpus"].append({
            "name": name,
            "vram_gb": round(vram / GB, 1) if vram else None,
            "driver": g.get("DriverVersion"),
            "driver_date": g.get("DriverDate"),
            "res": ("%s × %s" % (g.get("CurrentHorizontalResolution"), g.get("CurrentVerticalResolution"))
                    if g.get("CurrentHorizontalResolution") else None),
            "hz": g.get("CurrentRefreshRate"),
            "integrated": any(k in low for k in ("intel(r) uhd", "intel(r) hd", "iris", "radeon(tm) graphics", "vega")),
        })

    b, bi = raw.get("board") or {}, raw.get("bios") or {}
    s["board"] = {"maker": (b.get("Manufacturer") or "").strip(), "model": (b.get("Product") or "").strip(),
                  "bios": bi.get("SMBIOSBIOSVersion"), "bios_date": bi.get("ReleaseDate")}
    o = raw.get("os") or {}
    s["os"] = {"name": o.get("Caption") or "%s %s" % (platform.system(), platform.release()),
               "version": o.get("Version") or platform.version(), "build": o.get("BuildNumber"),
               "arch": o.get("OSArchitecture") or platform.machine(), "pc_name": platform.node()}

    parts = {}
    for p in _as_list(raw.get("parts")):
        parts.setdefault(str(p.get("DiskNumber")), []).append(p.get("Letter"))
    s["parts"] = parts
    for d in sorted(_as_list(raw.get("disks")), key=lambda x: int(x.get("DeviceId") or 0)):
        did = str(d.get("DeviceId"))
        media, bus = MEDIA.get(d.get("MediaType")), BUS.get(d.get("BusType"))
        if not media and bus == "NVMe":
            media = "SSD"
        s["disks"].append({"id": did, "name": (d.get("FriendlyName") or "").strip(), "media": media or "?",
                           "bus": bus or "?", "size_gb": round((d.get("Size") or 0) / 1e9),
                           "health": d.get("HealthStatus"), "letters": sorted(x for x in parts.get(did, []) if x)})
    if not s["disks"]:  # hors Windows : version simplifiée
        for i, p in enumerate(psutil.disk_partitions(all=False)):
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            s["disks"].append({"id": str(i), "name": p.device, "media": "?", "bus": "?",
                               "size_gb": round(u.total / 1e9), "health": None, "letters": [p.mountpoint]})

    for n in _as_list(raw.get("net")):
        s["net"].append({"name": n.get("Name"), "conn": n.get("NetConnectionID"),
                         "speed_mbps": round(int(n["Speed"]) / 1e6) if n.get("Speed") else None})
    for m in _as_list(raw.get("monitors")):
        label = " ".join(x for x in (m.get("Maker"), m.get("Name")) if x).strip()
        if label:
            s["monitors"].append(label)

    s["tips"] = []
    s["ready"] = True
    return s


# --------------------------------------------------------------------------
# Conseils en direct + analyse du composant limitant
# --------------------------------------------------------------------------
def idle_seconds():
    """Temps depuis la dernière action clavier/souris (Windows)."""
    if not IS_WIN:
        return 0
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LII(ctypes.sizeof(LII), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def startup_programs():
    """Programmes lancés au démarrage de Windows (activés)."""
    if not IS_WIN:
        return []
    import winreg
    names = []
    approved_base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"

    def disabled(hive, sub, name):
        try:
            with winreg.OpenKey(hive, approved_base + "\\" + sub) as k:
                v, _ = winreg.QueryValueEx(k, name)
                return bool(v) and v[0] % 2 == 1      # 03 = désactivé, 02/06 = activé
        except OSError:
            return False

    runs = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "Run32")]
    for hive, path, sub in runs:
        try:
            with winreg.OpenKey(hive, path) as k:
                i = 0
                while True:
                    try:
                        name, _v, _t = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if name and not disabled(hive, sub, name):
                        names.append(name)
        except OSError:
            pass
    for hive, folder in ((winreg.HKEY_CURRENT_USER, os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")),
                         (winreg.HKEY_LOCAL_MACHINE, os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs\Startup"))):
        try:
            for f in os.listdir(folder):
                if f.lower() != "desktop.ini" and not disabled(hive, "StartupFolder", f):
                    names.append(os.path.splitext(f)[0])
        except OSError:
            pass
    return names


def _gb(b, lang):
    v = "%.1f" % (b / GB)
    return (v.replace(".", ",") + " Go") if lang == "fr" else (v + " GB")


def _dur(sec, lang):
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    if sec < 60:
        return "%d s" % sec
    return ("%d h %02d min" % (h, m)) if h else ("%d min %02d s" % (m, sec % 60))


BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi")
IGNORED = ("system idle process", "idle", "registry", "memory compression", "pythonw", "python", "msedgewebview2")


class Insights:
    NEED_LOAD_S = 600   # 10 min de charge avant de donner un verdict

    def __init__(self, sampler):
        self.s = sampler
        self.hist = collections.deque(maxlen=120)
        self.top_cache, self.top_busy = {}, set()
        self.startup = []
        threading.Thread(target=self._load_startup, daemon=True).start()
        self.stats = self._load_stats()
        self._stats_saved = time.time()
        self._last_t = None
        self._bg_prev = None
        self.alerts = []
        self.loading_now = False
        self.now_vals = {"cpu": 0, "gpu": 0, "ram": 0, "vram": 0}

    # ---------- statistiques persistantes ----------
    @staticmethod
    def _empty():
        return {"load_s": 0.0, "gpu": 0.0, "cpu": 0.0, "ram": 0.0, "vram": 0.0, "since": date.today().isoformat()}

    def _load_stats(self):
        try:
            with open(STATS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            e = self._empty()
            return {k: (float(d.get(k, 0)) if k != "since" else str(d.get("since", e["since"]))) for k in e}
        except Exception:
            return self._empty()

    def save_stats(self):
        try:
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.stats, f)
            os.replace(tmp, STATS_FILE)
            self._stats_saved = time.time()
        except Exception:
            log.exception("stats.json")

    def reset_stats(self):
        self.stats = self._empty()
        self.save_stats()

    def _load_startup(self):
        try:
            self.startup = startup_programs()
        except Exception:
            log.exception("Startup programs")

    def _account(self, dt, cpu, maxcore, ram, gpu, vram_ratio):
        dt = min(max(dt, 0), 5)
        # moment « intensif » : un composant est assez sollicité pour pouvoir te limiter
        self.loading_now = cpu >= 40 or gpu >= 40 or ram >= 80 or vram_ratio >= 0.8
        self.now_vals = {"cpu": cpu, "gpu": gpu, "ram": ram, "vram": vram_ratio * 100}
        if not self.loading_now:          # usage léger : rien ne peut te limiter, on ne compte pas
            return
        st = self.stats
        st["load_s"] += dt
        if gpu >= 93:
            st["gpu"] += dt
        if (maxcore >= 95 or cpu >= 90) and gpu < 85:
            st["cpu"] += dt
        if ram >= 92:
            st["ram"] += dt
        if vram_ratio >= 0.95:
            st["vram"] += dt
        if time.time() - self._stats_saved > 60:
            self.save_stats()

    @staticmethod
    def _main_gpu(gpus):
        """La carte graphique dédiée la plus chargée (sinon n'importe laquelle)."""
        cands = [g for g in gpus if not g.get("integrated")] or gpus
        cands = [g for g in cands if g.get("usage") is not None]
        return max(cands, key=lambda g: g["usage"]) if cands else None

    # ---------- fenêtre ouverte : on part des mesures affichées ----------
    def on_live(self, d):
        now = d["t"]
        if self._last_t is None or now - self._last_t > 5:
            self.hist.clear()           # retour de veille : historique repart à zéro
        dt = now - self._last_t if self._last_t else 1.0
        self._last_t = now
        per = d["cpu"]["per"] or [0]
        g = self._main_gpu(d["gpus"])
        vr = (g["vram_used"] / g["vram_total"]) if g and g.get("vram_used") and g.get("vram_total") else 0
        snap = {"cpu": d["cpu"]["usage"], "core": max(per), "ram": d["ram"]["percent"],
                "swap": (d["ram"]["swap_used"] / d["ram"]["swap_total"] * 100) if d["ram"]["swap_total"] else 0,
                "gpus": {x["id"]: x for x in d["gpus"]}, "disks": {x["id"]: x for x in d["disks"]}}
        self.hist.append(snap)
        self._account(dt, snap["cpu"], snap["core"], snap["ram"], g["usage"] if g else 0, vr)
        try:
            self.alerts = self._alerts()
        except Exception:
            log.exception("Insights")
            self.alerts = []

    # ---------- fenêtre fermée : mesure légère toutes les 2 s ----------
    def on_background(self):
        now = time.time()
        cur = psutil.cpu_times(percpu=True)
        prev, self._bg_prev = self._bg_prev, cur
        if prev is None or len(prev) != len(cur):
            self._last_t = now
            return
        loads = []
        for a, b in zip(prev, cur):
            tot = sum(b) - sum(a)
            loads.append(100.0 * (1 - (b.idle - a.idle) / tot) if tot > 0 else 0.0)
        cpu, core = sum(loads) / len(loads), max(loads)
        ram = psutil.virtual_memory().percent
        gpu_use, gpu_mem, _ = self.s.counters.snapshot()
        best, vr = 0.0, 0.0
        specs = self.s.specs.get("gpus", [])
        for a in self.s.kmt.adapters:
            spec = next((x for x in specs if x["name"] == a["name"]), {})
            if spec.get("integrated"):
                continue
            u = gpu_use.get(a["luid"], 0.0)
            if u >= best:
                best = u
                tot = (spec.get("vram_gb") or 0) * GB
                vr = gpu_mem.get(a["luid"], (0, 0))[0] / tot if tot else 0
        dt = now - self._last_t if self._last_t else 2.0
        self._last_t = now
        self._account(dt, cpu, core, ram, best, vr)

    # ---------- programme le plus gourmand (mis en cache 15 s, calculé à part) ----------
    def top(self, kind, luid=None, sort=None):
        key = kind + (luid or "")
        c = self.top_cache.get(key)
        if (not c or time.time() - c[0] > 15) and key not in self.top_busy:
            self.top_busy.add(key)

            def job():
                try:
                    items = [i for i in self.s.details.compute(kind, luid, top=8)["items"]
                             if i["name"].lower() not in IGNORED]
                except Exception:
                    items = []
                self.top_cache[key] = (time.time(), items)
                self.top_busy.discard(key)
            threading.Thread(target=job, daemon=True).start()
        items = list(c[1]) if c else []
        if sort:
            items.sort(key=lambda i: i[sort], reverse=True)
        return items

    def _alerts(self):
        h = list(self.hist)
        out = []

        def win(n):
            return h[-n:] if len(h) >= n else None

        def mean(w, k):
            return sum(x[k] for x in w) / len(w)

        def add(aid, level, cat, fr, en, sig=None):
            # sig : valeur retenue quand on masque l'alerte (elle revient seulement si elle augmente)
            out.append({"id": aid, "level": level, "cat": cat, "fr": fr, "en": en, "sig": sig})

        w60, w30, w20 = win(60), win(30), win(20)
        # --- processeur ---
        if w60 and mean(w60, "cpu") >= 85:
            t = self.top("cpu")
            who = (" Le plus gourmand : %s (%d %%)." % (t[0]["name"], t[0]["cpu"]), " Biggest user: %s (%d%%)." % (t[0]["name"], t[0]["cpu"])) if t else ("", "")
            add("cpu_high", "warn", "cpu",
                "Processeur chargé à plus de 85 %% depuis 1 min.%s Ferme ce qui ne sert pas, ou vérifie qu'un programme n'est pas bloqué." % who[0],
                "CPU above 85%% for 1 min.%s Close what you don't need, or check that a program isn't stuck." % who[1])
        elif w30 and mean(w30, "core") >= 95 and mean(w30, "cpu") < 50:
            t = self.top("cpu")
            who = (t[0]["name"] + " est", t[0]["name"] + " is") if t else ("un programme est", "a program is")
            add("cpu_core", "info", "cpu",
                "Un cœur est saturé alors que le total est bas : %s limité par la vitesse d'un seul cœur. En jeu, baisse les réglages qui chargent le processeur (distance d'affichage, foule, physique)." % who[0],
                "One core is maxed while the total is low: %s limited by single-core speed. In games, lower CPU-heavy settings (view distance, crowds, physics)." % who[1])
        # --- mémoire ---
        if w30 and mean(w30, "ram") >= 88:
            t = self.top("ram")[:2]
            fr_who = " Gros consommateurs : " + ", ".join("%s (%s)" % (i["name"], _gb(i["ram"], "fr")) for i in t) + "." if t else ""
            en_who = " Biggest users: " + ", ".join("%s (%s)" % (i["name"], _gb(i["ram"], "en")) for i in t) + "." if t else ""
            br = any(i["name"].lower() in BROWSERS for i in t)
            add("ram_high", "warn", "ram",
                "RAM presque pleine (%d %%).%s Ferme ce qui ne sert pas%s." % (mean(w30, "ram"), fr_who,
                    " — pour le navigateur : ferme des onglets ou active l'économiseur de mémoire" if br else ""),
                "RAM almost full (%d%%).%s Close what you don't need%s." % (mean(w30, "ram"), en_who,
                    " — for the browser: close tabs or turn on memory saver" if br else ""))
            if mean(w30, "swap") >= 60:
                add("swap", "info", "ram",
                    "Windows se sert beaucoup du fichier d'échange (disque) comme RAM de secours : c'est ce qui rend le PC lent.",
                    "Windows is relying heavily on the page file (disk) as backup RAM: that's what makes the PC slow.")
        # --- carte graphique ---
        last = h[-1] if h else None
        for gid, g in (last["gpus"].items() if last else []):
            if g.get("integrated"):
                continue
            seq = [x["gpus"].get(gid) for x in (w20 or [])]
            if w20 and all(s and s.get("vram_used") and s.get("vram_total") for s in seq) and \
                    sum(s["vram_used"] / s["vram_total"] for s in seq) / len(seq) >= 0.92:
                t = self.top("gpu", gid, sort="vram")
                who = (" (surtout %s : %s)" % (t[0]["name"], _gb(t[0]["vram"], "fr")), " (mostly %s: %s)" % (t[0]["name"], _gb(t[0]["vram"], "en"))) \
                    if t and t[0]["vram"] else ("", "")
                add("vram:" + gid, "warn", "gpu",
                    "Mémoire vidéo presque pleine : %s / %s%s. Baisse la qualité des textures ou la résolution, sinon le jeu va saccader."
                    % (_gb(g["vram_used"], "fr"), _gb(g["vram_total"], "fr"), who[0]),
                    "Video memory almost full: %s / %s%s. Lower texture quality or resolution, or games will stutter."
                    % (_gb(g["vram_used"], "en"), _gb(g["vram_total"], "en"), who[1]))
            temps = [x["gpus"].get(gid, {}).get("temp") for x in (w30 or [])]
            if w30 and all(temps) and sum(temps) / len(temps) >= 85:
                add("hot:" + gid, "warn", "gpu",
                    "Carte graphique à %d °C depuis 30 s : vérifie la poussière, la circulation d'air dans le boîtier ou la courbe des ventilateurs." % (sum(temps) / len(temps)),
                    "Graphics card at %d °C for 30 s: check for dust, case airflow or the fan curve." % (sum(temps) / len(temps)))
        # --- disques ---
        for did, dk in (last["disks"].items() if last else []):
            act = [x["disks"].get(did, {}).get("active", 0) for x in (w30 or [])]
            if w30 and sum(act) / len(act) >= 90:
                t = self.top("disk")
                letters = ", ".join(l + ":" if len(l) == 1 else l for l in dk.get("letters") or []) or dk.get("name", "")
                who = (", surtout à cause de %s" % t[0]["name"], ", mostly because of %s" % t[0]["name"]) if t else ("", "")
                hdd = dk.get("media") == "HDD"
                add("disk:" + did, "warn", "disk",
                    "Disque %s (%s) occupé à 100 %% depuis 30 s%s.%s" % (did, letters, who[0], " C'est un disque dur (HDD) : pour les jeux et logiciels, un SSD éviterait ces ralentissements." if hdd else ""),
                    "Disk %s (%s) busy at 100%% for 30 s%s.%s" % (did, letters, who[1], " It's a hard drive (HDD): an SSD for games and software would avoid these slowdowns." if hdd else ""))
        # --- PC inactif mais occupé ---
        if w60 and mean(w60, "cpu") >= 15 and idle_seconds() >= 120:
            t = self.top("cpu")
            if t:
                add("bg_cpu", "info", "cpu",
                    "Tu n'utilises pas le PC, mais %s utilise %d %% du processeur en arrière-plan. Si ce n'est pas normal (mise à jour, antivirus…), ferme-le ou désactive son lancement au démarrage." % (t[0]["name"], t[0]["cpu"]),
                    "You're not using the PC, but %s is using %d%% of the CPU in the background. If that's unexpected (updates, antivirus…), close it or stop it from starting with Windows." % (t[0]["name"], t[0]["cpu"]))
        # --- mise à jour disponible (vérification automatique ou manuelle) ---
        if UPDATE_AVAIL[0]:
            add("update:" + UPDATE_AVAIL[0], "info", "update",
                "Nouvelle version de Pulse Monitor disponible : v%s. Clique sur le numéro de version en haut à gauche pour l'installer." % UPDATE_AVAIL[0],
                "New Pulse Monitor version available: v%s. Click the version number at the top left to install it." % UPDATE_AVAIL[0])
        # --- fin d'une session de jeu ---
        if GAMES[0]:
            for g in GAMES[0].recent_ended():
                fr, en = game_summary_text(g)
                add("game:%d" % g["start"], "info", "play", fr, en)
        # --- démarrage ---
        if len(self.startup) >= 8:
            ex = ", ".join(self.startup[:4])
            add("startup", "info", "startup",
                "%d programmes se lancent avec Windows (ex. : %s…). Désactive ceux dont tu n'as pas besoin : Gestionnaire des tâches → Applications de démarrage." % (len(self.startup), ex),
                "%d programs start with Windows (e.g. %s…). Disable the ones you don't need: Task Manager → Startup apps." % (len(self.startup), ex),
                sig=len(self.startup))
        return out

    def insight(self):
        st = self.stats
        L = st["load_s"]
        base = {"load_s": L, "need_s": self.NEED_LOAD_S, "since": st["since"], "bg": BG[0],
                "loading_now": self.loading_now, "now": self.now_vals,
                "shares": {k: (st[k] / L if L else 0) for k in ("gpu", "cpu", "ram", "vram")}}
        if L < self.NEED_LOAD_S:
            base.update(ready=False,
                        fr="Analyse en cours : %s de charge enregistrées sur les 10 min nécessaires. Joue ou utilise tes logiciels gourmands : l'usage léger ne compte pas." % _dur(L, "fr"),
                        en="Analysis in progress: %s of load recorded out of the 10 min needed. Play or use demanding software: light usage doesn't count." % _dur(L, "en"))
            return base
        sh = base["shares"]
        if sh["gpu"] >= 0.5 and sh["gpu"] >= 2 * sh["cpu"]:
            fr, en = ("C'est la carte graphique qui te limite le plus : c'est elle à changer en priorité pour gagner des FPS.",
                      "Your graphics card is the main limit: upgrade it first to gain FPS.")
        elif sh["cpu"] >= 0.25 and sh["cpu"] > sh["gpu"]:
            fr, en = ("C'est souvent le processeur qui limite : un CPU plus rapide (surtout en performance par cœur) aiderait.",
                      "The CPU is often the limit: a faster CPU (especially single-core performance) would help.")
        elif sh["vram"] >= 0.2:
            fr, en = ("La mémoire vidéo sature souvent : une carte avec plus de VRAM, ou des textures moins hautes, aiderait.",
                      "Video memory often fills up: a card with more VRAM, or lower textures, would help.")
        elif sh["ram"] >= 0.2:
            fr, en = ("La RAM sature souvent : plus de RAM aiderait.", "RAM often fills up: more RAM would help.")
        else:
            fr, en = ("Aucun composant ne bloque vraiment : ton PC est équilibré pour ton usage.",
                      "No component is really holding you back: your PC is balanced for your usage.")
        base.update(ready=True, fr=fr, en=en)
        return base


# --------------------------------------------------------------------------
# Mesures en direct
# --------------------------------------------------------------------------
class Sampler(threading.Thread):
    def __init__(self, counters, kmt):
        super().__init__(daemon=True)
        self.counters, self.kmt = counters, kmt
        self.selfuse = SelfUsage()
        self.details = ProcDetails(counters)
        self.specs = {"ready": False}
        self.lock = threading.Lock()
        self.latest = {}
        self._disk_prev = self._net_prev = self._t_prev = None
        self._vols = {}
        self._vols_t = 0.0
        self._sample_lock = threading.Lock()

    def run(self):
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        self.insights = Insights(self)
        n = 0
        seen = None
        while True:
            time.sleep(1)
            n += 1
            if not ui_idle():
                # on ne mesure qu'au rythme où la page demande (1 s sur Performances, 2 s ailleurs)
                if LAST_PING[0] == seen:
                    continue
                seen = LAST_PING[0]
            if ui_idle():
                self._t_prev = None  # on repart proprement au retour
                if BG[0] and n % 2 == 0:
                    try:
                        self.insights.on_background()
                    except Exception:
                        log.exception("Background analysis")
                continue
            try:
                data = self.sample()
                self.insights.on_live(data)
                data["alerts"] = self.insights.alerts
                data["insight"] = self.insights.insight()
                with self.lock:
                    self.latest = data
            except Exception:
                log.exception("Measurement error")

    def _volumes(self):
        # L'espace disque bouge lentement : on ne le relit que toutes les 10 s.
        now = time.time()
        if now - self._vols_t > 10:
            self._vols_t = now
            vols = {}
            for p in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                except Exception:
                    continue
                key = p.mountpoint.rstrip("\\/:")[:1].upper() if IS_WIN else p.mountpoint
                vols[key] = (u.used, u.total)
            self._vols = vols
        return self._vols

    def sample(self):
        with self._sample_lock:
            return self._sample()

    def _sample(self):
        now = time.time()
        dt = (now - self._t_prev) if self._t_prev else 1.0
        first = self._t_prev is None
        self._t_prev = now
        specs = self.specs

        usage = psutil.cpu_percent(None)
        per = psutil.cpu_percent(None, percpu=True)
        gpu_use, gpu_mem, cpu_perf = self.counters.snapshot()
        base = (specs.get("cpu") or {}).get("base_mhz")
        if cpu_perf and base:
            freq = base * cpu_perf / 100.0
        else:
            f = psutil.cpu_freq()
            freq = f.current if f else None
        try:
            procs = len(psutil.pids())
        except Exception:
            procs = None

        vm, sw = psutil.virtual_memory(), psutil.swap_memory()

        disks = []
        io = psutil.disk_io_counters(perdisk=True) or {}
        prev = io if first or not self._disk_prev else self._disk_prev
        parts_by_disk = specs.get("parts") or {}
        disk_meta = {d["id"]: d for d in specs.get("disks", [])}
        vols_all = self._volumes()
        for key, cur in sorted(io.items()):
            if IS_WIN:
                num = key.replace("PhysicalDrive", "")
            else:
                if key.startswith(("loop", "ram", "zram")) or (key[-1:].isdigit() and not key.startswith("nvme")):
                    continue
                num = key
            old = prev.get(key, cur)
            busy_ms = (cur.read_time - old.read_time) + (cur.write_time - old.write_time)
            meta = disk_meta.get(num, {})
            letters = sorted(parts_by_disk.get(num, []) or meta.get("letters", []))
            disks.append({
                "id": num, "name": meta.get("name") or key,
                "media": meta.get("media"), "bus": meta.get("bus"), "letters": letters,
                "active": max(0.0, min(100.0, busy_ms / (dt * 1000) * 100)),
                "read": max(0, (cur.read_bytes - old.read_bytes) / dt),
                "write": max(0, (cur.write_bytes - old.write_bytes) / dt),
                "volumes": [{"letter": l, "used": vols_all[l][0], "total": vols_all[l][1]}
                            for l in letters if l in vols_all],
            })
        self._disk_prev = io

        nets = []
        nio = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        nprev = nio if first or not self._net_prev else self._net_prev
        for name, cur in nio.items():
            st = stats.get(name)
            low = name.lower()
            if not st or not st.isup or "loopback" in low or low == "lo" or "vethernet" in low \
                    or "pseudo" in low or "isatap" in low or "teredo" in low:
                continue
            if cur.bytes_recv + cur.bytes_sent == 0:
                continue
            old = nprev.get(name, cur)
            nets.append({"name": name,
                         "down": max(0, (cur.bytes_recv - old.bytes_recv) / dt),
                         "up": max(0, (cur.bytes_sent - old.bytes_sent) / dt),
                         "link_mbps": st.speed or None})
        self._net_prev = nio

        gpus = []
        spec_gpus = specs.get("gpus", [])
        if self.kmt.adapters:
            for a in self.kmt.adapters:
                ded, sh = gpu_mem.get(a["luid"], (None, None))
                spec = next((g for g in spec_gpus if g["name"] == a["name"]), {})
                gpus.append({"id": a["luid"], "name": a["name"],
                             "usage": gpu_use.get(a["luid"], 0.0 if gpu_use else None),
                             "temp": self.kmt.temperature(a["handle"]),
                             "vram_used": ded, "shared_used": sh,
                             "vram_total": (spec.get("vram_gb") or 0) * GB or None,
                             "integrated": spec.get("integrated", False)})
        else:
            for i, g in enumerate(spec_gpus):
                gpus.append({"id": str(i), "name": g["name"], "usage": None, "temp": None,
                             "vram_used": None, "shared_used": None,
                             "vram_total": (g.get("vram_gb") or 0) * GB or None,
                             "integrated": g.get("integrated", False)})

        return {
            "t": now,
            "cpu": {"usage": usage, "per": per, "freq_mhz": freq, "procs": procs,
                    "uptime": now - psutil.boot_time(), "name": (specs.get("cpu") or {}).get("name")},
            "ram": {"used": vm.total - vm.available, "total": vm.total, "percent": vm.percent,
                    "available": vm.available, "swap_used": sw.used, "swap_total": sw.total},
            "gpus": gpus, "disks": disks, "net": nets,
            "app": self.selfuse.sample(),
            "update": UPDATE_AVAIL[0],
            "specs_ready": bool(specs.get("ready")),
        }


# --------------------------------------------------------------------------
# Analyse de l'espace disque (façon TreeSize) — ne tourne que quand on la lance
# --------------------------------------------------------------------------
CATEGORIES = {
    "video": "mp4 mkv avi mov wmv flv webm m4v mpg mpeg ts m2ts vob 3gp",
    "image": "jpg jpeg png gif bmp tif tiff webp heic raw cr2 nef arw dng psd svg ico",
    "audio": "mp3 flac wav aac ogg m4a wma opus aiff alac mid",
    "archive": "zip rar 7z tar gz bz2 xz iso img cab zst dmg vhd vhdx wim esd",
    "game": "exe dll sys pak vpk bin dat bundle assets resource ucas utoc big forge rpf wad pck cache msi",
    "document": "pdf doc docx xls xlsx ppt pptx odt ods txt rtf csv epub md",
    "dev": "py js ts java c cpp h cs go rs json xml html css lib obj pdb class jar whl node",
}
EXT_CAT = {e: c for c, exts in CATEGORIES.items() for e in exts.split()}
KEEP_CHILDREN_MIN = 64 * 1024 * 1024   # sous 64 Mo, on garde la taille d'un dossier mais pas son contenu
REPARSE = 0x400                          # FILE_ATTRIBUTE_REPARSE_POINT (jonctions, liens) : ignorés


class DiskScan:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"running": False}
        self.stop = False
        self.results = {}          # racine -> résultat (gardé tant que l'appli tourne)
        os.makedirs(SCANS_DIR, exist_ok=True)

    @staticmethod
    def _key(root):
        return "".join(ch if ch.isalnum() else "_" for ch in root)[:60] or "root"

    def drives(self):
        out = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in (p.opts or "") or not p.fstype:
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            f = os.path.join(SCANS_DIR, self._key(p.mountpoint) + ".json.gz")
            out.append({"root": p.mountpoint, "fs": p.fstype, "total": u.total, "used": u.used,
                        "scanned": os.path.getmtime(f) if os.path.exists(f) else None})
        return out

    def start(self, root):
        with self.lock:
            if self.status.get("running"):
                return False
            if not os.path.isdir(root):
                return False
            self.stop = False
            self.status = {"running": True, "root": root, "files": 0, "dirs": 0, "bytes": 0,
                           "started": time.time(), "path": root}
        threading.Thread(target=self._scan, args=(root,), daemon=True).start()
        return True

    def _scan(self, root):
        import heapq
        st = self.status
        # nœud = [nom, taille, nb fichiers, enfants (liste), taille des fichiers directs]
        top = [root.rstrip("\\/") or root, 0, 0, [], 0]
        stack = [(root, top)]
        big = []                                 # 60 plus gros fichiers
        cats = {c: [0, 0, []] for c in list(CATEGORIES) + ["other"]}
        denied = 0
        last = time.time()
        try:
            while stack:
                if self.stop:
                    raise InterruptedError
                path, node = stack.pop()
                try:
                    it = os.scandir(path)
                except OSError:
                    denied += 1
                    continue
                with it:
                    while True:
                        try:
                            e = next(it)
                        except StopIteration:
                            break
                        except OSError:          # dossier illisible en cours de route : on passe au suivant
                            denied += 1
                            break
                        try:
                            stt = e.stat(follow_symlinks=False)
                            if getattr(stt, "st_file_attributes", 0) & REPARSE:
                                continue
                            if e.is_dir(follow_symlinks=False):
                                child = [e.name, 0, 0, [], 0]
                                node[3].append(child)
                                stack.append((e.path, child))
                                st["dirs"] += 1
                            else:
                                size = stt.st_size
                                node[4] += size
                                node[2] += 1
                                st["files"] += 1
                                st["bytes"] += size
                                ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
                                c = cats[EXT_CAT.get(ext, "other")]
                                c[0] += size
                                c[1] += 1
                                if len(c[2]) < 15:
                                    heapq.heappush(c[2], (size, e.path))
                                elif size > c[2][0][0]:
                                    heapq.heapreplace(c[2], (size, e.path))
                                if len(big) < 60:
                                    heapq.heappush(big, (size, e.path))
                                elif size > big[0][0]:
                                    heapq.heapreplace(big, (size, e.path))
                        except OSError:
                            continue
                now = time.time()
                if now - last > 0.3:
                    last = now
                    st["path"] = path
            # tailles cumulées (parcours en profondeur, sans récursion)
            order, todo = [], [top]
            while todo:
                n = todo.pop()
                order.append(n)
                todo.extend(n[3])
            for n in reversed(order):
                n[1] = n[4] + sum(ch[1] for ch in n[3])
                n[2] += sum(ch[2] for ch in n[3])
            # élagage : on oublie le contenu des petits dossiers (garde la RAM basse)
            for n in order:
                if n[1] < KEEP_CHILDREN_MIN:
                    n[3] = []
                else:
                    n[3].sort(key=lambda ch: ch[1], reverse=True)
            try:
                used = psutil.disk_usage(root).used
            except Exception:
                used = top[1]
            res = {"root": root, "date": time.time(), "duration": time.time() - st["started"], "tree": top,
                   "used": used, "denied": denied,
                   "big": sorted(big, reverse=True),
                   "cats": {c: {"size": v[0], "count": v[1], "top": sorted(v[2], reverse=True)} for c, v in cats.items()}}
            self.results[root] = res
            try:
                import gzip
                with gzip.open(os.path.join(SCANS_DIR, self._key(root) + ".json.gz"), "wt", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                log.exception("Saving disk scan")
            st.update(running=False, done=True)
        except InterruptedError:
            st.update(running=False, stopped=True)
        except Exception:
            log.exception("Disk scan")
            st.update(running=False, error=True)
        finally:
            trim_memory()

    def result(self, root):
        if root in self.results:
            return self.results[root]
        f = os.path.join(SCANS_DIR, self._key(root) + ".json.gz")
        if os.path.exists(f):
            try:
                import gzip
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    self.results[root] = json.load(fh)
                return self.results[root]
            except Exception:
                log.exception("Reading disk scan")
        return None

    def node(self, root, path):
        """Contenu d'un dossier de l'analyse : path = liste de noms depuis la racine."""
        res = self.result(root)
        if not res:
            return None
        n = res["tree"]
        for name in path:
            n = next((ch for ch in n[3] if ch[0] == name), None)
            if n is None:
                return None
        kids = [{"name": ch[0], "size": ch[1], "files": ch[2], "open": bool(ch[3])} for ch in n[3][:300]]
        rest = n[3][300:]
        return {"root": root, "path": path, "size": n[1], "files": n[2], "direct": n[4], "children": kids,
                "more": {"count": len(rest), "size": sum(ch[1] for ch in rest)} if rest else None,
                "pruned": n[1] < KEEP_CHILDREN_MIN and n[1] > n[4]}

    def summary(self, root):
        res = self.result(root)
        if not res:
            return None
        return {k: res[k] for k in ("root", "date", "duration", "used", "denied", "big", "cats")} | {"total": res["tree"][1]}


def open_folder(path):
    """Ouvre ce dossier dans l'Explorateur (ou la corbeille / les réglages de stockage de Windows)."""
    if not IS_WIN:
        return False
    if path == "__data":                       # dossier de Pulse Monitor (réglages, historique, sessions…)
        path = DATA_DIR
    if path in ("shell:RecycleBinFolder", "ms-settings:storagesense"):
        os.startfile(path)
        return True
    if not os.path.isdir(path):
        return False
    subprocess.Popen(["explorer", os.path.normpath(path)])
    return True


def open_in_explorer(path):
    """Ouvre l'Explorateur sur ce fichier / dossier (sélectionné)."""
    if not IS_WIN or not os.path.exists(path):
        return False
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    return True


# --------------------------------------------------------------------------
# Mises à jour : UNIQUEMENT quand l'utilisateur clique sur « Vérifier » (aucune vérification automatique)
# --------------------------------------------------------------------------
UPDATE_KEEP = ("settings.json", "stats.json", "games.json", "games_lib.json", "pulse_monitor.log")   # données : jamais écrasées
UPDATE_EXT = (".py", ".html", ".bat", ".md", ".txt", ".ico", ".png", ".svg", ".css", ".js", ".json")


def _vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
    except ValueError:
        return (0,)


def _gh(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "PulseMonitor/" + APP_VERSION, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def update_check():
    """Version du code publié sur GitHub (branche principale) : un simple « push » suffit, pas besoin de release."""
    if not UPDATE_REPO:
        return {"error": "norepo", "current": APP_VERSION}
    try:
        with _gh("https://raw.githubusercontent.com/%s/HEAD/pulse_monitor.py" % UPDATE_REPO) as r:
            head = r.read(6000).decode("utf-8", "ignore")
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', head)
        if not m:
            raise ValueError("APP_VERSION introuvable")
        latest = m.group(1)
    except Exception as e:
        log.warning("Update check: %s", e)
        code = getattr(e, "code", None)            # erreur HTTP : GitHub a bien répondu
        err = "norelease" if code == 404 else "rate" if code in (403, 429) else "net"
        return {"error": err, "current": APP_VERSION}
    notes = ""
    try:                                           # message du dernier envoi (facultatif)
        with _gh("https://api.github.com/repos/%s/commits?per_page=1" % UPDATE_REPO) as r:
            c = json.loads(r.read().decode("utf-8"))
        notes = (c[0]["commit"]["message"] if c else "")[:4000]
    except Exception:
        pass
    return {"current": APP_VERSION, "latest": latest, "newer": _vtuple(latest) > _vtuple(APP_VERSION), "notes": notes, "exe": FROZEN,
            "zip": "https://api.github.com/repos/%s/zipball" % UPDATE_REPO}


UPDATE_STATE = {"busy": False, "error": None, "done": False}
UPDATE_AVAIL = [None]   # version plus récente trouvée par la vérification automatique


def auto_update_loop():
    """Option (désactivée par défaut) : une vérification par jour, rien d'autre n'est envoyé."""
    while True:
        try:
            st = load_settings()
            if st.get("auto_update") and time.time() - float(st.get("update_last") or 0) >= 86400:
                info = update_check()
                if not info.get("error"):
                    update_settings(update_last=time.time())
                    UPDATE_AVAIL[0] = info.get("latest") if info.get("newer") else None
        except Exception:
            log.exception("Automatic update check")
        time.sleep(1800)


def update_install():
    """Télécharge la dernière version, garde une copie de l'ancienne dans backup/, remplace les fichiers, relance l'appli."""
    if UPDATE_STATE["busy"]:
        return
    if FROZEN:                     # version .exe : on ouvre la page de téléchargement de la dernière version
        webbrowser.open("https://github.com/%s/releases/latest" % UPDATE_REPO)
        UPDATE_STATE.update(busy=False, error=None, done=False, opened=True)
        return
    UPDATE_STATE.update(busy=True, error=None, done=False)

    def job():
        import io
        import shutil
        import zipfile
        try:
            info = update_check()
            if info.get("error") or not info.get("zip"):
                raise RuntimeError(info.get("error") or "nozip")
            with _gh(info["zip"], timeout=60) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            files = []
            for n in z.namelist():
                parts = n.split("/", 1)                 # 1er dossier = « propriétaire-dépôt-commit/ »
                if len(parts) < 2 or not parts[1] or n.endswith("/"):
                    continue
                rel = parts[1]
                if ".." in rel.split("/") or rel.startswith((".git", "history/", "scans/", "backup/", "docs/")) or rel in UPDATE_KEEP:
                    continue
                if not rel.lower().endswith(UPDATE_EXT):
                    continue
                files.append((n, rel))
            if not any(rel == "pulse_monitor.py" for _, rel in files):
                raise RuntimeError("badzip")
            bdir = os.path.join(BASE_DIR, "backup")
            for _, rel in files:                          # copie de sécurité de la version actuelle
                src = os.path.join(BASE_DIR, *rel.split("/"))
                if os.path.isfile(src):
                    dst = os.path.join(bdir, *rel.split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
            for n, rel in files:
                dst = os.path.join(BASE_DIR, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(n) as fsrc, open(dst + ".new", "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                os.replace(dst + ".new", dst)
            log.info("Update installed: %s -> %s", APP_VERSION, info.get("latest"))
            UPDATE_STATE.update(done=True)
            time.sleep(1.5)                               # laisse la page afficher « redémarrage… »
            subprocess.Popen(self_cmd("--wait-pid", str(os.getpid()), "--open"),
                             cwd=BASE_DIR, creationflags=(0x00000008 | 0x00000200) if IS_WIN else 0)   # DETACHED + NEW_GROUP
            if QUIT_HOOK[0]:
                QUIT_HOOK[0]()
            os._exit(0)
        except Exception as e:
            log.exception("Update")
            UPDATE_STATE.update(error=str(e) or "err")
        UPDATE_STATE["busy"] = False
    threading.Thread(target=job, daemon=True).start()


# --------------------------------------------------------------------------
# Fichiers temporaires et caches : taille seulement (rien n'est supprimé)
# --------------------------------------------------------------------------
def _env(k):
    return os.environ.get(k) or ""


def _globdirs(base, *subs):
    """Dossiers base/<profil>/sub pour chaque profil de navigateur (Default, Profile 1…)."""
    out = []
    try:
        for e in os.scandir(base):
            if e.is_dir():
                for sub in subs:
                    p = os.path.join(e.path, sub)
                    if os.path.isdir(p):
                        out.append(p)
    except OSError:
        pass
    return out


def _steam_dir():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return os.path.normpath(winreg.QueryValueEx(k, "SteamPath")[0])
    except Exception:
        return ""


def temp_groups():
    L, R, W = _env("LOCALAPPDATA"), _env("APPDATA"), _env("SystemRoot") or r"C:\Windows"
    tmp = _env("TEMP")
    steam = _steam_dir()
    chromium = lambda base: _globdirs(base, "Cache", "Code Cache", "GPUCache")
    g = [
        ("win_temp", [tmp, os.path.join(W, "Temp")]),
        ("updates", [os.path.join(W, "SoftwareDistribution", "Download")]),
        ("browsers", chromium(os.path.join(L, "Google", "Chrome", "User Data")) + chromium(os.path.join(L, "Microsoft", "Edge", "User Data"))
                     + chromium(os.path.join(L, "BraveSoftware", "Brave-Browser", "User Data"))
                     + _globdirs(os.path.join(L, "Mozilla", "Firefox", "Profiles"), "cache2")
                     + [os.path.join(L, "Opera Software", "Opera Stable", "Cache"), os.path.join(L, "Opera Software", "Opera GX Stable", "Cache")]),
        ("apps", [os.path.join(R, "discord", d) for d in ("Cache", "Code Cache", "GPUCache")]
                 + [os.path.join(R, "Spotify", "Data"), os.path.join(L, "Spotify", "Data")]
                 + ([os.path.join(steam, "appcache", "httpcache"), os.path.join(L, "Steam", "htmlcache")] if steam else [os.path.join(L, "Steam", "htmlcache")])),
        ("shaders", [os.path.join(L, "D3DSCache"), os.path.join(L, "NVIDIA", "DXCache"), os.path.join(L, "NVIDIA", "GLCache"),
                     os.path.join(L, "AMD", "DxCache"), os.path.join(L, "AMD", "DxcCache"), os.path.join(L, "AMD", "VkCache"),
                     os.path.join(L, "Intel", "ShaderCache")] + ([os.path.join(steam, "steamapps", "shadercache")] if steam else [])),
        ("thumbs", [os.path.join(L, "Microsoft", "Windows", "Explorer")]),
        ("crash", [os.path.join(L, "CrashDumps"), os.path.join(W, "Minidump"), os.path.join(L, "Microsoft", "Windows", "WER"),
                   os.path.join(_env("ProgramData") or r"C:\ProgramData", "Microsoft", "Windows", "WER")]),
    ]
    out, seen = [], set()
    for gid, paths in g:
        ps = []
        for p in paths:
            n = os.path.normcase(os.path.normpath(p)) if p else ""
            if n and n not in seen and os.path.isdir(p):
                seen.add(n)
                ps.append(os.path.normpath(p))
        out.append((gid, ps))
    return out


def _dir_size(path, stop):
    tot, files, stack = 0, 0, [path]
    while stack and not stop():
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not (e.stat(follow_symlinks=False).st_file_attributes & REPARSE if IS_WIN else e.is_symlink()):
                                stack.append(e.path)
                        else:
                            tot += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return tot, files


def recycle_bin_size():
    if not IS_WIN:
        return 0, 0
    from ctypes import wintypes

    class SHQ(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]
    q = SHQ(); q.cbSize = ctypes.sizeof(SHQ)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(q)) == 0:
        return q.i64Size, q.i64NumItems
    return 0, 0


class TempScan:
    def __init__(self):
        self.data, self.busy, self.lock = None, False, threading.Lock()

    def _run(self):
        t0 = time.time()
        groups = []
        try:
            for gid, paths in temp_groups():
                items = []
                for p in paths:
                    size, files = _dir_size(p, lambda: time.time() - t0 > 120)
                    items.append({"path": p, "size": size, "files": files})
                items.sort(key=lambda i: -i["size"])
                groups.append({"id": gid, "size": sum(i["size"] for i in items), "items": items})
            rb, rn = recycle_bin_size()
            groups.append({"id": "recycle", "size": rb, "items": [{"path": "shell:RecycleBinFolder", "size": rb, "files": rn}]})
        except Exception:
            log.exception("Temporary files")
        groups.sort(key=lambda g: -g["size"])
        self.data = {"groups": groups, "total": sum(g["size"] for g in groups), "date": time.time(), "duration": time.time() - t0}
        self.busy = False

    def get(self, refresh=False):
        with self.lock:
            if (refresh or self.data is None) and not self.busy:
                self.busy = True
                threading.Thread(target=self._run, daemon=True).start()
        d = dict(self.data) if self.data else {"groups": [], "total": 0, "date": None}
        d["busy"] = self.busy
        return d


TEMPSCAN = TempScan()


# --------------------------------------------------------------------------
# Historique : 1 point par minute, gardé 8 jours (fichiers par jour dans history/)
# --------------------------------------------------------------------------
HIST_LIVE = [None]   # l'historique en cours (pour la minute pas encore enregistrée)


class History(threading.Thread):
    STEP = 5          # une mesure légère toutes les 5 s, résumée chaque minute
    KEEP_DAYS = 8

    def __init__(self, sampler, kmt):
        super().__init__(daemon=True)
        self.s, self.kmt = sampler, kmt
        self.prev = None
        self.acc = []
        self.minute = None
        self.psnap = None      # relevé des programmes de la minute précédente (pour leur CPU moyen sur la minute)
        self.ncpu = psutil.cpu_count() or 1
        HIST_LIVE[0] = self
        os.makedirs(HIST_DIR, exist_ok=True)

    def _measure(self):
        cur = psutil.cpu_times(percpu=True)
        cpu, core = 0.0, 0.0
        if self.prev and len(self.prev) == len(cur):
            loads = []
            for a, b in zip(self.prev, cur):
                tot = sum(b) - sum(a)
                loads.append(max(0.0, 100.0 * (1 - (b.idle - a.idle) / tot)) if tot > 0 else 0.0)
            cpu, core = sum(loads) / len(loads), max(loads)
        self.prev = cur
        ram = psutil.virtual_memory().percent
        gpu_use, gpu_mem, _ = self.s.counters.snapshot()
        specs = self.s.specs.get("gpus", []) if isinstance(self.s.specs, dict) else []
        gpu, temp, vram, luid = 0.0, None, 0.0, None
        for a in self.kmt.adapters:
            spec = next((x for x in specs if x["name"] == a["name"]), {})
            if spec.get("integrated"):
                continue
            u = gpu_use.get(a["luid"], 0.0)
            if luid is None or u > gpu:
                gpu, luid = u, a["luid"]
                temp = self.kmt.temperature(a["handle"])
                tot = (spec.get("vram_gb") or 0) * GB
                vram = gpu_mem.get(a["luid"], (0, 0))[0] / tot * 100 if tot else 0.0
        return {"c": cpu, "k": core, "r": ram, "g": gpu, "T": temp, "v": vram, "luid": luid}

    def _top_names(self, C, G, r, luid):
        p = {}
        try:
            if C >= 60:
                t = [i for i in self.s.details.compute("cpu", top=4)["items"] if i["name"].lower() not in IGNORED]
                if t:
                    p["cpu"] = t[0]["name"]
            if r >= 85:
                t = [i for i in self.s.details.compute("ram", top=4)["items"] if i["name"].lower() not in IGNORED]
                if t:
                    p["ram"] = t[0]["name"]
            if G >= 60 and luid:
                by = self.s.counters.gpu_by_pid(luid)
                if by:
                    pid = max(by, key=by.get)
                    try:
                        n = psutil.Process(pid).name()
                        p["gpu"] = n[:-4] if n.lower().endswith(".exe") else n
                    except psutil.Error:
                        pass
        except Exception:
            log.exception("History: programs")
        return p

    def _top3(self, luid):
        """Les 10 programmes qui ont le plus utilisé le CPU (moyenne sur la minute), la RAM et le GPU.
        Un seul relevé des programmes par minute : ~quelques dizaines de ms de CPU."""
        now, cur = ProcDetails._snap()
        prev, self.psnap = self.psnap, (now, cur)
        dt = now - prev[0] if prev else 0
        gpu = self.s.counters.gpu_by_pid(luid) if luid else {}
        groups = {}
        for pid, (name, cpu, ram, _rd, _wr) in cur.items():
            low = name.lower()
            key = low[:-4] if low.endswith(".exe") else low
            if key in IGNORED:
                continue
            g = groups.setdefault(key, [name[:-4] if low.endswith(".exe") else name, 0.0, 0, 0.0])
            g[2] += ram or 0
            old = prev[1].get(pid) if prev else None
            if old and old[1] is not None and cpu is not None and 20 < dt < 180:
                g[1] += max(0.0, (cpu - old[1]) / dt / self.ncpu * 100)
            g[3] += gpu.get(pid, 0.0)
        items = list(groups.values())
        top = lambda i, nd, minv: [[x[0], round(min(100.0, x[i]), nd) if i != 2 else round(x[i] / 1048576)]
                                   for x in sorted(items, key=lambda x: x[i], reverse=True)[:10] if x[i] > minv]
        d = {"c": top(1, 1, 0.05), "r": top(2, 0, 0), "g": top(3, 1, 0.05)}
        return {k: v for k, v in d.items() if v}

    def live_point(self):
        """La minute en cours (pas encore écrite) : la courbe va jusqu'au bord droit du graphique."""
        a = list(self.acc)
        if not a or not HIST_ON[0]:
            return None
        avg = lambda k: round(sum(x[k] for x in a) / len(a), 1)
        mx = lambda k: round(max(x[k] for x in a), 1)
        temps = [x["T"] for x in a if x["T"]]
        return {"t": int(time.time()), "c": avg("c"), "C": mx("c"), "g": avg("g"), "G": mx("g"),
                "T": round(max(temps), 1) if temps else None, "r": avg("r"), "v": mx("v")}

    def _flush(self, minute):
        a = self.acc
        self.acc = []
        if not a:
            return
        avg = lambda k: round(sum(x[k] for x in a) / len(a), 1)
        mx = lambda k: round(max(x[k] for x in a), 1)
        temps = [x["T"] for x in a if x["T"]]
        pt = {"t": minute * 60, "c": avg("c"), "C": mx("c"), "g": avg("g"), "G": mx("g"),
              "T": round(max(temps), 1) if temps else None, "r": avg("r"), "v": mx("v")}
        p = self._top_names(pt["C"], pt["G"], pt["r"], a[-1]["luid"])
        if p:
            pt["p"] = p
        try:
            d = self._top3(a[-1]["luid"])
            if d:
                pt["d"] = d
        except Exception:
            log.exception("History: program details")
        day = time.strftime("%Y-%m-%d", time.localtime(minute * 60))
        try:
            with open(os.path.join(HIST_DIR, day + ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(pt, separators=(",", ":")) + "\n")
        except Exception:
            log.exception("History: write")

    def _cleanup(self):
        limit = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.KEEP_DAYS * 86400))
        try:
            for f in os.listdir(HIST_DIR):
                if f.endswith(".jsonl") and f[:10] < limit:
                    os.remove(os.path.join(HIST_DIR, f))
        except OSError:
            pass

    def run(self):
        self._cleanup()
        n = 0
        while True:
            time.sleep(self.STEP)
            if not HIST_ON[0] and not GAMES_ON[0]:
                self.acc, self.prev = [], None
                continue
            try:
                first = self.prev is None          # la 1re mesure ne sert qu'à démarrer le calcul du CPU
                m = self._measure()
                if GAMES[0] and not first:
                    try:
                        GAMES[0].tick(m, self.STEP)
                    except Exception:
                        log.exception("Gaming sessions")
                if not HIST_ON[0]:
                    self.acc = []
                    continue
                minute = int(time.time() // 60)
                if self.minute is not None and minute != self.minute:
                    self._flush(self.minute)
                self.minute = minute
                if not first and len(self.acc) < 30:
                    self.acc.append(m)
                n += 1
                if n % 720 == 0:
                    self._cleanup()
            except Exception:
                log.exception("History")


# --------------------------------------------------------------------------
# Sessions de jeu : détection légère (toutes les 5 s, avec les mesures de l'historique)
# --------------------------------------------------------------------------
GAME_DIRS = ("\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\world of warcraft\\", "\\gog galaxy\\games\\",
             "\\gog games\\", "\\ubisoft game launcher\\games\\", "\\ea games\\", "\\electronic arts\\", "\\xboxgames\\",
             "\\battle.net\\games\\", "\\call of duty\\", "\\overwatch\\", "\\diablo", "\\hearthstone\\",
             "\\starcraft", "\\minecraft", "\\games\\")
NOT_GAMES = ("launcher", "battle.net", "steam", "epicgames", "riotclient", "galaxyclient", "ubisoftconnect", "upc", "eadesktop",
             "crashreport", "crashhandler", "unitycrash", "easyanticheat", "beservice", "vanguard", "setup", "install",
             "update", "agent", "helper", "leagueclient", "crashpad", "unins", "redist", "vcredist", "dxsetup", "cefprocess", "webview", "overlay", "discord", "obs", "explorer", "pythonw", "python",
             "msedge", "chrome", "firefox", "opera", "brave", "vivaldi", "msedgewebview2", "applicationframehost",
             "adobe premiere pro", "afterfx", "photoshop", "resolve", "blender", "handbrake", "mediaencoder", "vegas",
             "lightroom", "davinci", "nvcontainer", "radeonsoftware", "amdrsserv", "textinputhost", "searchhost", "shellexperiencehost")


def _fg_window():
    """(pid, plein écran ?) de la fenêtre au premier plan."""
    if not IS_WIN:
        return None, False
    from ctypes import wintypes
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None, False
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rc = wintypes.RECT()
    full = False
    if u.GetWindowRect(hwnd, ctypes.byref(rc)):
        class MI(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mi = MI(); mi.cbSize = ctypes.sizeof(MI)
        mon = u.MonitorFromWindow(hwnd, 2)
        if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            m = mi.rcMonitor   # plein écran ou fenêtré sans bordure : la fenêtre couvre tout l'écran
            full = rc.left <= m.left and rc.top <= m.top and rc.right >= m.right and rc.bottom >= m.bottom
    return pid.value, full


# --------------------------------------------------------------------------
# Images par seconde des jeux : événements « Present » de Windows (ETW), lus directement (comme PresentMon)
# Nécessite les droits administrateur OU le groupe « Utilisateurs du journal de performances » (S-1-5-32-559).
# --------------------------------------------------------------------------
class FrameCounter:
    SESSION = "PulseMonitorFPS"
    # fournisseurs : (GUID, id de l'événement « une image affichée », mots-clés, type)
    PROVIDERS = (
        ("{CA11C036-0102-4A2D-A6AD-F03CFED5D3C9}", 42, 0xFFFFFFFFFFFFFFFF, 0),    # DXGI : Present_Start (DirectX 10–12)
        ("{783ACA0A-790E-4D7F-8451-AA850511C6B9}", 1, 0xFFFFFFFFFFFFFFFF, 1),     # Direct3D 9 : Present_Start
        ("{802EC45A-1E99-4B83-9920-87C98277BA9D}", 184, 0x8000000, 2),            # DxgKrnl : Present (Vulkan, OpenGL…)
    )

    def __init__(self):
        self.state = "unknown"          # ok | no_rights | error | unsupported | unknown
        self.lock = threading.Lock()
        self.targets = frozenset()
        self.ts = {}                    # (pid, type) -> [horodatages QPC]
        self.last = {}                  # (pid, type) -> dernier horodatage (entre deux relevés)
        self.th = None
        self.handle = None
        self.trace = None
        self._ids = {}
        if not IS_WIN:
            self.state = "unsupported"
            return
        import ctypes.wintypes as W
        self.W = W
        f = ctypes.c_longlong()
        ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(f))
        self.qpf = f.value or 10_000_000
        self._define()

    # ---- structures Windows ----
    def _define(self):
        W, C = self.W, ctypes

        class GUID(C.Structure):
            _fields_ = [("Data1", C.c_ulong), ("Data2", C.c_ushort), ("Data3", C.c_ushort), ("Data4", C.c_ubyte * 8)]

        class WNODE_HEADER(C.Structure):
            _fields_ = [("BufferSize", C.c_ulong), ("ProviderId", C.c_ulong), ("HistoricalContext", C.c_uint64),
                        ("TimeStamp", C.c_int64), ("Guid", GUID), ("ClientContext", C.c_ulong), ("Flags", C.c_ulong)]

        class EVENT_TRACE_PROPERTIES(C.Structure):
            _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", C.c_ulong), ("MinimumBuffers", C.c_ulong),
                        ("MaximumBuffers", C.c_ulong), ("MaximumFileSize", C.c_ulong), ("LogFileMode", C.c_ulong),
                        ("FlushTimer", C.c_ulong), ("EnableFlags", C.c_ulong), ("AgeLimit", C.c_long),
                        ("NumberOfBuffers", C.c_ulong), ("FreeBuffers", C.c_ulong), ("EventsLost", C.c_ulong),
                        ("BuffersWritten", C.c_ulong), ("LogBuffersLost", C.c_ulong), ("RealTimeBuffersLost", C.c_ulong),
                        ("LoggerThreadId", C.c_void_p), ("LogFileNameOffset", C.c_ulong), ("LoggerNameOffset", C.c_ulong)]

        class EVENT_DESCRIPTOR(C.Structure):
            _fields_ = [("Id", C.c_ushort), ("Version", C.c_ubyte), ("Channel", C.c_ubyte), ("Level", C.c_ubyte),
                        ("Opcode", C.c_ubyte), ("Task", C.c_ushort), ("Keyword", C.c_uint64)]

        class EVENT_HEADER(C.Structure):
            _fields_ = [("Size", C.c_ushort), ("HeaderType", C.c_ushort), ("Flags", C.c_ushort), ("EventProperty", C.c_ushort),
                        ("ThreadId", C.c_ulong), ("ProcessId", C.c_ulong), ("TimeStamp", C.c_int64), ("ProviderId", GUID),
                        ("EventDescriptor", EVENT_DESCRIPTOR), ("ProcessorTime", C.c_uint64), ("ActivityId", GUID)]

        class EVENT_RECORD(C.Structure):
            _fields_ = [("EventHeader", EVENT_HEADER), ("BufferContext", C.c_ulong), ("ExtendedDataCount", C.c_ushort),
                        ("UserDataLength", C.c_ushort), ("ExtendedData", C.c_void_p), ("UserData", C.c_void_p),
                        ("UserContext", C.c_void_p)]

        class EVENT_TRACE_HEADER(C.Structure):
            _fields_ = [("Size", C.c_ushort), ("FieldTypeFlags", C.c_ushort), ("Version", C.c_ulong), ("ThreadId", C.c_ulong),
                        ("ProcessId", C.c_ulong), ("TimeStamp", C.c_int64), ("Guid", GUID), ("ProcessorTime", C.c_uint64)]

        class EVENT_TRACE(C.Structure):
            _fields_ = [("Header", EVENT_TRACE_HEADER), ("InstanceId", C.c_ulong), ("ParentInstanceId", C.c_ulong),
                        ("ParentGuid", GUID), ("MofData", C.c_void_p), ("MofLength", C.c_ulong), ("ClientContext", C.c_ulong)]

        class SYSTEMTIME(C.Structure):
            _fields_ = [(n, C.c_ushort) for n in ("Y", "M", "DW", "D", "H", "Mi", "S", "Ms")]

        class TIME_ZONE_INFORMATION(C.Structure):
            _fields_ = [("Bias", C.c_long), ("StandardName", C.c_wchar * 32), ("StandardDate", SYSTEMTIME),
                        ("StandardBias", C.c_long), ("DaylightName", C.c_wchar * 32), ("DaylightDate", SYSTEMTIME),
                        ("DaylightBias", C.c_long)]

        class TRACE_LOGFILE_HEADER(C.Structure):
            _fields_ = [("BufferSize", C.c_ulong), ("Version", C.c_ulong), ("ProviderVersion", C.c_ulong),
                        ("NumberOfProcessors", C.c_ulong), ("EndTime", C.c_int64), ("TimerResolution", C.c_ulong),
                        ("MaximumFileSize", C.c_ulong), ("LogFileMode", C.c_ulong), ("BuffersWritten", C.c_ulong),
                        ("LogInstanceGuid", GUID), ("LoggerName", C.c_void_p), ("LogFileName", C.c_void_p),
                        ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", C.c_int64), ("PerfFreq", C.c_int64),
                        ("StartTime", C.c_int64), ("ReservedFlags", C.c_ulong), ("BuffersLost", C.c_ulong)]

        self.CALLBACK = C.WINFUNCTYPE(None, C.POINTER(EVENT_RECORD))

        class EVENT_TRACE_LOGFILEW(C.Structure):
            _fields_ = [("LogFileName", C.c_wchar_p), ("LoggerName", C.c_wchar_p), ("CurrentTime", C.c_int64),
                        ("BuffersRead", C.c_ulong), ("ProcessTraceMode", C.c_ulong), ("CurrentEvent", EVENT_TRACE),
                        ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", C.c_void_p), ("BufferSize", C.c_ulong),
                        ("Filled", C.c_ulong), ("EventsLost", C.c_ulong), ("EventRecordCallback", self.CALLBACK),
                        ("IsKernelTrace", C.c_ulong), ("Context", C.c_void_p)]

        self.GUID, self.PROPS, self.LOGFILE = GUID, EVENT_TRACE_PROPERTIES, EVENT_TRACE_LOGFILEW
        a = C.WinDLL("advapi32")
        a.StartTraceW.argtypes = [C.POINTER(C.c_uint64), C.c_wchar_p, C.c_void_p]
        a.ControlTraceW.argtypes = [C.c_uint64, C.c_wchar_p, C.c_void_p, C.c_ulong]
        a.EnableTraceEx2.argtypes = [C.c_uint64, C.POINTER(GUID), C.c_ulong, C.c_ubyte, C.c_uint64, C.c_uint64,
                                     C.c_ulong, C.c_void_p]
        a.OpenTraceW.argtypes = [C.POINTER(EVENT_TRACE_LOGFILEW)]
        a.OpenTraceW.restype = C.c_uint64
        a.ProcessTrace.argtypes = [C.POINTER(C.c_uint64), C.c_ulong, C.c_void_p, C.c_void_p]
        a.CloseTrace.argtypes = [C.c_uint64]
        self.api = a
        # GUID des fournisseurs -> type (comparaison rapide sur Data1)
        self._kinds = {}
        for g, eid, _kw, kind in self.PROVIDERS:
            self._kinds[int(g[1:9], 16)] = (eid, kind)

    @staticmethod
    def _guid(GUID, s):
        import uuid
        u = uuid.UUID(s)
        g = GUID()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g

    def _props(self):
        name_bytes = (len(self.SESSION) + 1) * 2
        buf = ctypes.create_string_buffer(ctypes.sizeof(self.PROPS) + name_bytes + 1024)
        p = ctypes.cast(buf, ctypes.POINTER(self.PROPS)).contents
        p.Wnode.BufferSize = len(buf)
        p.Wnode.Flags = 0x00020000            # WNODE_FLAG_TRACED_GUID
        p.Wnode.ClientContext = 1             # horodatage QPC
        p.LogFileMode = 0x00000100            # EVENT_TRACE_REAL_TIME_MODE
        p.BufferSize = 64                     # Ko
        p.MinimumBuffers, p.MaximumBuffers = 4, 16
        p.FlushTimer = 1
        p.LoggerNameOffset = ctypes.sizeof(self.PROPS)
        return buf

    def _stop_session(self):
        buf = self._props()
        self.api.ControlTraceW(0, self.SESSION, buf, 1)       # EVENT_TRACE_CONTROL_STOP

    # ---- droits ----
    def probe(self):
        """Vérifie (une fois) qu'on a le droit de lire ces événements : ouvre et referme une session."""
        if self.state == "unsupported":
            return self.state
        try:
            self._stop_session()
            h = ctypes.c_uint64()
            r = self.api.StartTraceW(ctypes.byref(h), self.SESSION, self._props())
            if r == 0:
                self._stop_session()
                self.state = "ok"
            elif r == 5:                       # ERROR_ACCESS_DENIED
                self.state = "no_rights"
            else:
                self.state = "error"
                log.warning("FPS: StartTrace returned %s", r)
        except Exception:
            self.state = "error"
            log.exception("FPS: permission check")
        admin = False
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass
        log.info("FPS: capture rights = %s (app running as administrator: %s)", self.state, "yes" if admin else "no")
        return self.state

    # ---- capture ----
    def _on_event(self, rec):
        h = rec.contents.EventHeader
        pid = h.ProcessId
        if pid not in self.targets:
            return
        k = self._kinds.get(h.ProviderId.Data1)
        if not k or h.EventDescriptor.Id != k[0]:
            return
        lst = self.ts.get((pid, k[1]))
        if lst is None:
            lst = self.ts[(pid, k[1])] = []
        lst.append(h.TimeStamp)

    def _start(self):
        self._stop_session()
        h = ctypes.c_uint64()
        r = self.api.StartTraceW(ctypes.byref(h), self.SESSION, self._props())
        if r != 0:
            self.state = "no_rights" if r == 5 else "error"
            if r != 5:
                log.warning("FPS: could not start capture (%s)", r)
            return False
        self.handle = h.value
        for g, _eid, kw, _k in self.PROVIDERS:
            gg = self._guid(self.GUID, g)
            self.api.EnableTraceEx2(self.handle, ctypes.byref(gg), 1, 4, kw, 0, 0, None)  # ENABLE, niveau « info »
        lf = self.LOGFILE()
        lf.LoggerName = self.SESSION
        lf.ProcessTraceMode = 0x00000100 | 0x10000000      # REAL_TIME | EVENT_RECORD
        self._cb = self.CALLBACK(self._on_event)             # garder une référence (sinon libéré)
        lf.EventRecordCallback = self._cb
        self._lf = lf
        t = self.api.OpenTraceW(ctypes.byref(lf))
        if t == 0xFFFFFFFFFFFFFFFF:
            log.warning("FPS: OpenTrace failed (%s)", ctypes.GetLastError())
            self._stop_session()
            self.handle = None
            self.state = "error"
            return False
        self.trace = t

        def run():
            tr = ctypes.c_uint64(t)
            try:
                self.api.ProcessTrace(ctypes.byref(tr), 1, None, None)   # bloque jusqu'à l'arrêt de la session
            except Exception:
                log.exception("FPS: reading events")
            finally:
                self.api.CloseTrace(t)
        self.th = threading.Thread(target=run, daemon=True, name="fps")
        self.th.start()
        self.state = "ok"
        log.info("FPS: capture started")
        return True

    def _stop(self):
        if self.handle is None:
            return
        self._stop_session()                  # ProcessTrace se termine tout seul
        self.handle = None
        self.trace = None
        self.ts, self.last = {}, {}
        log.info("FPS: capture stopped")

    def set_targets(self, pids):
        """Processus des jeux en cours. Vide = capture arrêtée (aucun coût hors des sessions de jeu)."""
        if self.state in ("unsupported", "no_rights"):
            return
        pids = frozenset(pids)
        self.targets = pids
        try:
            if pids and self.handle is None:
                self._start()
            elif not pids and self.handle is not None:
                self._stop()
        except Exception:
            log.exception("FPS")

    def take(self, pids):
        """Intervalles entre images (ms) depuis le dernier relevé, pour le processus du jeu qui en affiche le plus."""
        if self.handle is None:
            return []
        best = []
        ts, self.ts = self.ts, {}            # échange atomique (GIL) : le rappel remplit le nouveau dictionnaire
        for pid in pids:
            for kind in (0, 1, 2):            # DXGI, sinon D3D9, sinon le noyau (Vulkan/OpenGL)
                lst = ts.get((pid, kind))
                if not lst:
                    continue
                prev = self.last.get((pid, kind))
                self.last[(pid, kind)] = lst[-1]
                seq = ([prev] if prev else []) + lst
                iv = [(b - a) * 1000.0 / self.qpf for a, b in zip(seq, seq[1:])]
                iv = [x for x in iv if 0 < x < 1000]              # > 1 s : jeu en pause / réduit, pas une « image lente »
                if len(iv) > len(best):
                    best = iv
                break
        return best


def fps_stats(iv):
    """FPS moyen et « 1 % low » (FPS des 1 % d'images les plus lentes) à partir des intervalles en ms."""
    if len(iv) < 10:
        return None, None
    tot = sum(iv)
    s = sorted(iv)
    k = max(1, len(s) // 100)
    slow = s[-k:]
    return round(len(iv) * 1000 / tot, 1), round(1000 / (sum(slow) / len(slow)), 1)


FPS = FrameCounter()
FPS_ON = [True]


def fps_grant():
    """Ajoute le compte Windows au groupe « Utilisateurs du journal de performances » (une seule fois, avec l'accord
    de l'administrateur). Il faut ensuite se déconnecter / reconnecter pour que Windows en tienne compte."""
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    u = user.replace("'", "''")
    # déjà membre : pas une erreur
    cmd = ("if (-not (Get-LocalGroupMember -SID 'S-1-5-32-559' | Where-Object { $_.Name -eq '%s' })) "
           "{ Add-LocalGroupMember -SID 'S-1-5-32-559' -Member '%s' }" % (u, u))
    ok = run_elevated_ps(cmd)
    if ok:
        update_settings(fps_granted=True)
        log.info("FPS: account added to the Performance Log Users group")
    return ok


# --------------------------------------------------------------------------
# Jeux installés (recherche à la demande) + jeux ajoutés à la main
# --------------------------------------------------------------------------
KNOWN_GAMES = {"league of legends.exe", "valorant-win64-shipping.exe", "fortniteclient-win64-shipping.exe", "r5apex.exe",
               "r5apex_dx12.exe", "cs2.exe", "csgo.exe", "dota2.exe", "wow.exe", "wowclassic.exe", "overwatch.exe", "destiny2.exe",
               "eldenring.exe", "gta5.exe", "gta5_enhanced.exe", "rocketleague.exe", "cod.exe", "cod24-cod.exe", "rainbowsix.exe",
               "rainbowsix_be.exe", "eafc25.exe", "eafc26.exe", "pubg-win64-shipping.exe", "tslgame.exe", "diablo iv.exe",
               "genshinimpact.exe", "starrail.exe", "zenlesszonezero.exe", "pathofexile.exe", "pathofexile_x64steam.exe",
               "pathofexile2.exe", "warframe.x64.exe", "minecraft.windows.exe", "robloxplayerbeta.exe", "marvel-win64-shipping.exe",
               "deadlock.exe", "hearthstone.exe", "sc2_x64.exe", "cyberpunk2077.exe", "bg3.exe", "bg3_dx11.exe", "helldivers2.exe"}
GAME_PUBLISHERS = ("riot games", "blizzard", "electronic arts", "ubisoft", "rockstar", "cd projekt", "bethesda", "mojang",
                   "bandai namco", "square enix", "capcom", "sega", "2k", "activision", "epic games, inc", "valve", "gog",
                   "hoyoverse", "cognosphere", "wargaming", "grinding gear", "digital extremes", "bungie", "krafton", "paradox")
NOT_GAME_TITLES = ("launcher", "battle.net", "redistributable", "steamworks", "vanguard", "riot client", "anti-cheat", "anticheat",
                   "easyanticheat", "directx", "physx", "ea app", "origin", "ubisoft connect", "galaxy", "proton", "steamvr", "runtime", "sdk")


def _game_exes(folder, limit=15):
    """Les .exe d'un dossier de jeu (3 niveaux max), sans les installeurs / lanceurs / outils."""
    out, stack, seen = [], [(folder, 0)], 0
    while stack and len(out) < limit and seen < 4000:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < 3:
                                stack.append((e.path, depth + 1))
                        elif e.name.lower().endswith(".exe"):
                            low = e.name.lower()[:-4]
                            if not any(x in low for x in NOT_GAMES):
                                out.append(e.name.lower())
                    except OSError:
                        pass
        except OSError:
            pass
    return out[:limit]


def scan_installed_games():
    found = {}

    def add(name, folder, src):
        if not name or not folder or not os.path.isdir(folder):
            return
        if any(x in name.lower() for x in NOT_GAME_TITLES):
            return
        key = os.path.normcase(os.path.normpath(folder))
        if key not in found:
            found[key] = {"name": name.strip()[:80], "dir": os.path.normpath(folder), "src": src}

    # Steam : bibliothèques + manifestes
    steam = _steam_dir()
    if steam:
        libs = [steam]
        try:
            with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="ignore") as f:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
        for lib in dict.fromkeys(os.path.normcase(os.path.normpath(x)) for x in libs):
            sa = os.path.join(lib, "steamapps")
            try:
                for e in os.scandir(sa):
                    if e.name.startswith("appmanifest_") and e.name.endswith(".acf"):
                        try:
                            with open(e.path, encoding="utf-8", errors="ignore") as f:
                                t = f.read()
                            n = re.search(r'"name"\s+"([^"]+)"', t)
                            d = re.search(r'"installdir"\s+"([^"]+)"', t)
                            if n and d:
                                add(n.group(1), os.path.join(sa, "common", d.group(1)), "Steam")
                        except OSError:
                            pass
            except OSError:
                pass
    # Epic Games
    man = os.path.join(_env("ProgramData") or r"C:\ProgramData", "Epic", "EpicGamesLauncher", "Data", "Manifests")
    try:
        for e in os.scandir(man):
            if e.name.endswith(".item"):
                try:
                    with open(e.path, encoding="utf-8", errors="ignore") as f:
                        j = json.load(f)
                    add(j.get("DisplayName"), j.get("InstallLocation"), "Epic")
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    if IS_WIN:
        import winreg
        # GOG
        for root in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as g:
                                add(winreg.QueryValueEx(g, "gameName")[0], winreg.QueryValueEx(g, "path")[0], "GOG")
                        except OSError:
                            pass
            except OSError:
                pass
        # Programmes installés : éditeurs de jeux connus (Riot, Blizzard, EA, Ubisoft…) et jeux Steam
        for hive, root in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                           (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                           (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
            try:
                with winreg.OpenKey(hive, root) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            kn = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, kn) as g:
                                val = lambda n: (winreg.QueryValueEx(g, n)[0] if n else "")
                                try:
                                    pub = str(val("Publisher")).lower()
                                except OSError:
                                    pub = ""
                                if kn.startswith("Steam App ") or any(x in pub for x in GAME_PUBLISHERS):
                                    try:
                                        loc = val("InstallLocation")
                                    except OSError:
                                        loc = ""
                                    add(val("DisplayName"), loc, "Steam" if kn.startswith("Steam App ") else "Windows")
                        except OSError:
                            pass
            except OSError:
                pass
    games = sorted(found.values(), key=lambda g: g["name"].lower())
    for g in games:
        g["exes"] = _game_exes(g["dir"])
    return games


def windowed_apps():
    """Programmes ouverts avec une fenêtre visible (pour « ajouter un jeu »)."""
    if not IS_WIN:
        return []
    from ctypes import wintypes
    u = ctypes.windll.user32
    pids = set()
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0 and not u.GetWindow(hwnd, 4):   # 4 = GW_OWNER
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True
    u.EnumWindows(PROC(cb), 0)
    out = {}
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            n = psutil.Process(pid).name()
        except psutil.Error:
            continue
        if n.lower() not in ("explorer.exe", "applicationframehost.exe", "textinputhost.exe", "systemsettings.exe", "msedgewebview2.exe"):
            out[n.lower()] = n
    return sorted(out.values(), key=str.lower)


class GameLib:
    def __init__(self):
        self.games, self.manual, self.date, self.busy = [], [], None, False
        self.dirs, self.exes = (), set()
        try:
            with open(GAMELIB_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.games = [g for g in d.get("games", []) if isinstance(g, dict)]
            self.manual = [str(x).lower() for x in d.get("manual", [])][:100]
            self.date = d.get("date")
        except Exception:
            pass
        self._index()

    def _index(self):
        self.dirs = tuple(os.path.normcase(os.path.normpath(g["dir"])).lower() + os.sep for g in self.games if g.get("dir"))
        self.exes = {e for g in self.games for e in g.get("exes", [])}

    def _save(self):
        try:
            tmp = GAMELIB_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"games": self.games, "manual": self.manual, "date": self.date}, f, ensure_ascii=False)
            os.replace(tmp, GAMELIB_FILE)
        except Exception:
            log.exception("games_lib.json")

    def scan(self):
        if self.busy:
            return
        self.busy = True

        def job():
            try:
                self.games = scan_installed_games()
                self.date = time.time()
                self._index()
                self._save()
            except Exception:
                log.exception("Installed games search")
            self.busy = False
        threading.Thread(target=job, daemon=True).start()

    def add(self, name):
        n = name.strip().lower()[:80]
        if n and n not in self.manual:
            self.manual.append(n)
            self._save()

    def remove(self, name):
        n = name.strip().lower()
        if n in self.manual:
            self.manual.remove(n)
            self._save()

    def data(self):
        return {"games": [{k: g[k] for k in ("name", "src", "dir")} for g in self.games], "manual": self.manual,
                "date": self.date, "busy": self.busy}


GAMELIB = GameLib()


class GameTracker:
    START_SURE = 10     # jeu connu / trouvé par la recherche / ajouté à la main : session ouverte après 10 s
    START_GUESS = 30    # deviné (plein écran + carte graphique chargée) : 30 s pour éviter les fausses alertes
    MIN_S = 180         # sessions de moins de 3 min : pas gardées
    KEEP = 40
    SHOW_S = 6 * 3600   # le résumé reste dans la cloche 6 h

    def __init__(self, sampler):
        self.s = sampler
        # tout est groupé par NOM de programme : un programme à plusieurs processus (Firefox, Chrome…) = une seule session
        self.cand = {}       # nom -> secondes cumulées avant d'ouvrir une session
        self.ignore = {}     # nom -> nom affiché : sessions mises en pause à la main (reprise possible)
        self.tick_lock = threading.RLock()
        self.curs = {}       # nom -> session en cours (plusieurs jeux / programmes en même temps)
        self.names = {}      # pid -> (nom, chemin)
        self.sessions = self._load()
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(GAMES_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _save(self):
        try:
            tmp = GAMES_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.sessions[-self.KEEP:], f, separators=(",", ":"))
            os.replace(tmp, GAMES_FILE)
        except Exception:
            log.exception("games.json")

    def _info(self, pid):
        if pid not in self.names:
            name, exe = "", ""
            try:
                p = psutil.Process(pid)
                name = p.name()
                exe = p.exe() or ""        # refusé pour les jeux protégés par un anti-triche (ex. League of Legends)
            except psutil.Error:
                pass
            if len(self.names) > 300:
                self.names.clear()
            self.names[pid] = (name, exe)
        return self.names[pid]

    SCAN_S = 15        # liste complète des programmes ouverts : au plus toutes les 15 s (coûteuse)

    def _running_names(self, force=False):
        """Noms (en minuscules) des programmes ouverts -> (nom d'origine, pids). Mis en cache 15 s."""
        now = time.time()
        if force or now - getattr(self, "_run_t", 0) >= self.SCAN_S:
            out = {}
            for p in psutil.process_iter(["name"]):
                n = p.info.get("name") or ""
                if n:
                    e = out.setdefault(n.lower(), [n, set()])
                    e[1].add(p.pid)
            self._run, self._run_t = out, now
        return self._run

    def _is_game(self, pid, full, gpu_pid):
        if not pid or pid == os.getpid() or pid == getattr(self.s.selfuse, "window_pid", None):
            return False
        name, exe = self._info(pid)
        nl = name.lower()
        if nl and nl in GAMELIB.manual:                     # ajouté à la main : toujours un jeu
            return 2
        if nl in KNOWN_GAMES:                                # jeux connus (même protégés par un anti-triche)
            return 2 if gpu_pid >= 5 else 0
        low = nl[:-4] if nl.endswith(".exe") else nl
        if not low or any(x in low for x in NOT_GAMES):
            return False
        el = exe.lower()
        if nl in GAMELIB.exes or (el and any(el.startswith(d) for d in GAMELIB.dirs)):   # jeu trouvé par la recherche
            return 2 if gpu_pid >= 5 else 0
        in_dir = any(d in el for d in GAME_DIRS)
        return 1 if (in_dir and gpu_pid >= 10) or (full and gpu_pid >= 25) else 0

    def tick(self, m, dt):
        with self.tick_lock:
            self._tick(m, dt)

    def _tick(self, m, dt):
        luid = m.get("luid")
        by = None
        now = time.time()
        need = self.curs or self.ignore or self.cand or GAMELIB.manual
        running = self._running_names() if need else {}
        # 1) sessions en cours : on ajoute la mesure, on termine celles dont le programme est fermé (tous ses processus)
        for key, c in list(self.curs.items()):
            pids = c.setdefault("pids", set(running.get(key, ("", set()))[1]))
            pids &= {p for p in pids if psutil.pid_exists(p)}          # vérification légère des processus connus
            if not pids:                                                # plus aucun : on revérifie la liste complète
                running = self._running_names(force=True)
                pids |= running.get(key, ("", set()))[1]
            if not pids:
                self._end(key, now)
                continue
            c["n"] += 1
            for k in ("c", "g", "r", "v"):
                c["s" + k] += m[k] or 0
                c["m" + k] = max(c["m" + k], m[k] or 0)
            if m.get("T"):
                c["mT"] = max(c["mT"], m["T"])
                c["sT"] += m["T"]; c["nT"] += 1
            if m["g"] >= 93:
                c["bg"] += 1
            if (m["k"] >= 95 or m["c"] >= 90) and m["g"] < 85:
                c["bc"] += 1
            if m["r"] >= 92:
                c["br"] += 1
            if m["v"] >= 95:
                c["bv"] += 1
            c["end"] = now
            iv = FPS.take(pids)
            if iv:
                c["ft"].extend(iv)                    # images de la minute en cours
                c["fn"] += len(iv)
                c["fms"] += sum(iv)
                for x in iv:                          # répartition des temps d'image (par 0,5 ms) : pour le 1 % low de la session
                    b = min(399, int(x * 2))
                    c["fh"][b] = c["fh"].get(b, 0) + 1
                c["fps_now"] = fps_stats(iv)[0]
            else:
                c["fps_now"] = None
            c["acc"].append(m)
            if len(c["acc"]) * dt >= 60:              # 1 point par minute pour les graphiques de la session
                self._flush_tl(c)
        # 2) nouveaux candidats : le jeu au premier plan + tous les programmes ajoutés à la main
        self.ignore = {k: n for k, n in self.ignore.items() if k in running}
        cands = {}
        pid, full = _fg_window()
        if pid:
            name, exe = self._info(pid)
            key = name.lower()
            if key in self.curs:
                self.curs[key].setdefault("pids", set()).add(pid)
            elif key and key not in self.ignore:
                if by is None:                          # utilisation GPU par programme : calculée seulement si besoin
                    by = self.s.counters.gpu_by_pid(luid) if luid else {}
                lvl = self._is_game(pid, full, by.get(pid, 0.0))
                if lvl:
                    cands[key] = (lvl, name, exe, pid)
        for key in GAMELIB.manual:
            if key in running and key not in self.curs and key not in self.ignore:
                cands.setdefault(key, (2, running[key][0], "", None))
        for key in list(self.cand):
            if key not in cands and key not in running:
                del self.cand[key]
        for key, (lvl, name, exe, cpid) in cands.items():
            n = self.cand.get(key, 0) + dt
            self.cand[key] = n
            if n >= (self.START_SURE if lvl == 2 else self.START_GUESS):
                self._open(key, name, exe, now - n, now)
                if cpid:
                    self.curs[key]["pids"].add(cpid)
                self.cand.pop(key, None)
        # 3) mesure des FPS : seulement pendant les sessions de jeu
        FPS.set_targets(set().union(*[c.get("pids", set()) for c in self.curs.values()]) if FPS_ON[0] else set())

    def _open(self, key, name, exe, start, now):
        start = int(start)
        used = {int(c["start"]) for c in self.curs.values()} | {g.get("start") for g in self.sessions}
        while start in used:                              # identifiant unique (2 sessions ouvertes la même seconde)
            start += 1
        self.curs[key] = {"key": key, "name": name[:-4] if name.lower().endswith(".exe") else name, "exe": exe,
                          "start": start, "end": now, "n": 0, "nT": 0, "sT": 0.0, "mT": 0.0,
                          "bg": 0, "bc": 0, "br": 0, "bv": 0, "acc": [], "tl": [],
                          "ft": [], "fn": 0, "fms": 0.0, "fh": {}, "fmax": 0.0, "fps_now": None,
                          "pids": set(getattr(self, "_run", {}).get(key, ("", set()))[1]),
                          **{p + k: 0.0 for p in ("s", "m") for k in ("c", "g", "r", "v")}}

    @staticmethod
    def _flush_tl(c):
        a, c["acc"] = c["acc"], []
        if not a:
            return
        avg = lambda k: round(sum((x[k] or 0) for x in a) / len(a), 1)
        mx = lambda k: round(max((x[k] or 0) for x in a), 1)
        temps = [x["T"] for x in a if x.get("T")]
        f_avg, f_low = fps_stats(c.get("ft") or [])
        c["ft"] = []
        if f_avg:
            c["fmax"] = max(c.get("fmax", 0.0), f_avg)
        c["tl"].append([int(time.time() - c["start"]), avg("c"), mx("c"), avg("g"), mx("g"),
                        round(max(temps), 1) if temps else None, avg("r"), mx("v"), f_avg, f_low])
        if len(c["tl"]) > 600:                        # très longue session : on regroupe 2 par 2
            t = c["tl"]
            mean = lambda x, y: round((x + y) / 2, 1) if x is not None and y is not None else (x if y is None else y)
            low = lambda x, y: min([v for v in (x, y) if v is not None], default=None)
            c["tl"] = [[b[0], round((a_[1] + b[1]) / 2, 1), max(a_[2], b[2]), round((a_[3] + b[3]) / 2, 1), max(a_[4], b[4]),
                        max([x for x in (a_[5], b[5]) if x is not None], default=None), round((a_[6] + b[6]) / 2, 1), max(a_[7], b[7]),
                        mean(a_[8] if len(a_) > 8 else None, b[8] if len(b) > 8 else None),
                        low(a_[9] if len(a_) > 9 else None, b[9] if len(b) > 9 else None)]
                       for a_, b in zip(t[0::2], t[1::2])] + ([t[-1]] if len(t) % 2 else [])

    def stop(self, key=None):
        """Pause à la main : la session est gardée tout de suite (quelle que soit sa durée), le programme n'est plus suivi
        tant qu'on ne relance pas le suivi (ou jusqu'à sa fermeture)."""
        with self.tick_lock:
            if key is None and len(self.curs) == 1:
                key = next(iter(self.curs))
            c = self.curs.get(key)
            if not c:
                return None
            self.ignore[key] = c["name"]
            self._end(key, time.time(), force=True)
            return int(c["start"])

    def resume(self, key):
        """Relance le suivi : nouvelle session tout de suite."""
        with self.tick_lock:
            if key in self.ignore:
                name = self.ignore.pop(key)
                now = time.time()
                self._open(key, name, "", now, now)

    def _end(self, key, now, force=False):
        c = self.curs.pop(key, None)
        if not c:
            return
        self._flush_tl(c)
        dur = c["end"] - c["start"]
        if (dur < self.MIN_S and not force) or not c["n"]:
            return
        n = c["n"]
        g = {"name": c["name"], "start": int(c["start"]), "end": int(c["end"]), "dur": int(dur),
             "cpu": round(c["sc"] / n), "cpu_max": round(c["mc"]), "gpu": round(c["sg"] / n), "gpu_max": round(c["mg"]),
             "ram_max": round(c["mr"]), "vram_max": round(c["mv"]),
             "temp": round(c["sT"] / c["nT"]) if c["nT"] else None, "temp_max": round(c["mT"]) if c["mT"] else None,
             "limit": {"gpu": c["bg"] / n, "cpu": c["bc"] / n, "ram": c["br"] / n, "vram": c["bv"] / n},
             "tl": c["tl"]}
        if c.get("fn", 0) >= 100:                     # FPS de toute la session : moyenne, 1 % low, meilleure minute
            g["fps"] = round(c["fn"] * 1000 / c["fms"])
            need, acc, slow_ms, cnt = max(1, c["fn"] // 100), 0, 0.0, 0
            for b in sorted(c["fh"], reverse=True):  # les 1 % d'images les plus lentes
                k = min(c["fh"][b], need - cnt)
                slow_ms += k * (b + 0.5) / 2
                cnt += k
                if cnt >= need:
                    break
            g["fps_low"] = round(1000 / (slow_ms / cnt)) if cnt else None
            g["fps_max"] = round(c.get("fmax") or 0) or None
        with self.lock:
            self.sessions.append(g)
            self.sessions = self.sessions[-self.KEEP:]
        self._save()

    def recent_ended(self):
        t = time.time()
        return [g for g in self.sessions[-3:] if t - g["end"] < self.SHOW_S]

    def data(self):
        cur = [{"key": k, "name": c["name"], "start": int(c["start"]), "dur": int(c["end"] - c["start"]),
                "fps": round(c["fps_now"]) if c.get("fps_now") else None}
               for k, c in sorted(self.curs.items(), key=lambda x: x[1]["start"])]
        paused = [{"key": k, "name": n} for k, n in self.ignore.items()]
        with self.lock:
            ss = [{**{k: v for k, v in g.items() if k != "tl"}, "has_tl": bool(g.get("tl"))} for g in reversed(self.sessions)]
        return {"sessions": ss, "current": cur, "paused": paused, "on": GAMES_ON[0],
                "fps": {"state": FPS.state, "on": FPS_ON[0], "granted": bool(load_settings().get("fps_granted"))}}

    def timeline(self, start):
        with self.lock:
            g = next((g for g in self.sessions if g.get("start") == start), None)
        return {"tl": (g or {}).get("tl") or []}

    def clear(self):
        with self.lock:
            self.sessions = []
        self._save()

    def delete(self, start):
        with self.lock:
            self.sessions = [g for g in self.sessions if g.get("start") != start]
        self._save()


def game_verdict(g):
    L = g.get("limit") or {}
    if L.get("vram", 0) >= 0.2:
        return "vram"
    if L.get("cpu", 0) >= 0.25 and L.get("cpu", 0) > L.get("gpu", 0):
        return "cpu"
    if L.get("ram", 0) >= 0.2:
        return "ram"
    if L.get("gpu", 0) >= 0.4:
        return "gpu"
    return "none"


def game_summary_text(g):
    v = game_verdict(g)
    vf = {"gpu": "la carte graphique était à fond (normal en jeu)", "cpu": "le processeur limitait souvent",
          "ram": "la RAM était souvent pleine", "vram": "la mémoire vidéo saturait", "none": "rien ne limitait ton PC"}[v]
    ve = {"gpu": "the graphics card was maxed out (normal in games)", "cpu": "the CPU was often the limit",
          "ram": "RAM was often full", "vram": "video memory was saturated", "none": "nothing was holding your PC back"}[v]
    tf = " · max %d °C" % g["temp_max"] if g.get("temp_max") else ""
    return ("Session terminée : %s, %s. GPU %d %% en moyenne, CPU %d %%%s. Verdict : %s. Détails dans l'onglet Gaming." % (g["name"], _dur(g["dur"], "fr"), g["gpu"], g["cpu"], tf, vf),
            "Session ended: %s, %s. GPU %d%% average, CPU %d%%%s. Verdict: %s. Details in the Gaming tab." % (g["name"], _dur(g["dur"], "en"), g["gpu"], g["cpu"], tf.replace(" · max", " · max"), ve))


def history_at(t):
    """Le point de l'historique le plus proche de l'instant t (± 90 s), avec le détail des programmes."""
    best, bd = None, 91
    for day in {time.strftime("%Y-%m-%d", time.localtime(t + o)) for o in (-90, 0, 90)}:
        try:
            with open(os.path.join(HIST_DIR, day + ".jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        pt = json.loads(line)
                    except ValueError:
                        continue
                    dd = abs(pt.get("t", 0) - t)
                    if dd < bd:
                        best, bd = pt, dd
        except OSError:
            pass
    return best


def _hist_files():
    try:
        return sorted(f for f in os.listdir(HIST_DIR) if f.endswith(".jsonl"))
    except OSError:
        return []


def _hist_read(f, keep):
    out = []
    try:
        with open(os.path.join(HIST_DIR, f), encoding="utf-8") as fh:
            for line in fh:
                try:
                    pt = json.loads(line)
                except ValueError:
                    continue
                if keep(pt.get("t", 0)):
                    if "d" in pt:                      # détail des programmes : lu au clic (history_at), trop lourd ici
                        del pt["d"]
                        pt["hd"] = 1
                    out.append(pt)
    except OSError:
        pass
    return out


def read_history(view, end=None, compact=False, before=None, after=0):
    """Points autour de la vue qui se termine à « end » (maintenant par défaut) : « before » heures avant, « after » après
    (réserve pour glisser). compact : heures de mesures (les périodes éteintes ne comptent pas), sinon heures d'horloge.
    Regroupés par 10 min quand la vue dépasse 24 h. Renvoie (points, pas, {t0, t1, more, first})."""
    now = time.time()
    live = end is None or end >= now - 60
    end = now if live else end
    before = before or view
    after = 0 if live else after
    files = _hist_files()
    first = None
    if files:
        try:
            with open(os.path.join(HIST_DIR, files[0]), encoding="utf-8") as fh:
                first = json.loads(fh.readline()).get("t")
        except (OSError, ValueError):
            pass
    day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
    if not compact:
        since, until = end - before * 3600, min(now, end + after * 3600)
        pts = []
        for f in files:
            if day(since) <= f[:10] <= day(until):
                pts += _hist_read(f, lambda t: since <= t <= until)
        pts.sort(key=lambda p: p["t"])
        # dernière mesure avant / première après la réserve : pour dessiner une coupure qui commence ou finit hors de la vue
        lim0 = pts[0]["t"] if pts else until
        prev = nxt = None
        for f in reversed(files):
            if f[:10] > day(lim0):
                continue
            ts = [p["t"] for p in _hist_read(f, lambda t: t < lim0)]
            if ts:
                prev = max(ts)
                break
        if until < now - 60:
            lim1 = pts[-1]["t"] if pts else since
            for f in files:
                if f[:10] < day(lim1):
                    continue
                ts = [p["t"] for p in _hist_read(f, lambda t: t > lim1)]
                if ts:
                    nxt = min(ts)
                    break
        meta = {"t0": since, "t1": now if until >= now - 60 else until, "more": bool(first and first < since), "first": first,
                "prev": prev, "next": nxt}
    else:
        nb, na = int(before * 60), int(after * 60)
        old, new = [], []
        for f in reversed(files):                       # avant « end » : du plus récent au plus ancien
            if f[:10] > day(end):
                continue
            if len(old) >= nb:
                break
            old = _hist_read(f, lambda t: t <= end) + old
        more = len(old) > nb
        old = old[-nb:] if nb else []
        if na:
            for f in files:                             # après « end » : du plus ancien au plus récent
                if f[:10] < day(end):
                    continue
                if len(new) >= na:
                    break
                new += _hist_read(f, lambda t: t > end)
        reached = len(new) <= na                        # on a tout jusqu'à maintenant
        new = new[:na]
        pts = old + new
        t0 = pts[0]["t"] if pts else end
        t1 = now if (live or reached) else (new[-1]["t"] if new else end)
        meta = {"t0": t0, "t1": t1, "more": more, "first": first}
    if view <= 24:
        return pts, 60, meta
    step = 600
    out, bucket, key = [], [], None
    for pt in pts + [None]:
        k = pt["t"] // step if pt else None
        if bucket and k != key:
            avg = lambda f: round(sum(x[f] for x in bucket) / len(bucket), 1)
            mx = lambda f: max((x[f] for x in bucket if x.get(f) is not None), default=None)
            peak = max(bucket, key=lambda x: x["C"])
            agg = {"t": key * step, "c": avg("c"), "C": mx("C"), "g": avg("g"), "G": mx("G"), "T": mx("T"),
                   "r": avg("r"), "v": mx("v")}
            progs = {}
            for x in sorted(bucket, key=lambda x: x["C"], reverse=True):
                for kk, vv in (x.get("p") or {}).items():
                    progs.setdefault(kk, vv)
            if progs:
                agg["p"] = progs
            if peak.get("hd"):
                agg["hd"], agg["dt"] = 1, peak["t"]       # détail : celui du moment le plus chargé des 10 min
            out.append(agg)
            bucket = []
        if pt:
            key = k
            bucket.append(pt)
    return out, step, meta


# --------------------------------------------------------------------------
# Santé des disques (S.M.A.R.T. via Windows) — lue à la demande, gardée en cache
# --------------------------------------------------------------------------
HEALTH_PS = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$st = 'root/Microsoft/Windows/Storage'
# via CIM directement (Get-PhysicalDisk ne répond pas sur certaines versions allégées de Windows)
$out = @(Get-CimInstance -Namespace $st -ClassName MSFT_PhysicalDisk | ForEach-Object {
    $d = $_
    $r = Get-CimAssociatedInstance -InputObject $d -ResultClassName MSFT_StorageReliabilityCounter
    if (-not $r) { $r = $d | Get-StorageReliabilityCounter }
    [pscustomobject]@{
        id = [string]$d.DeviceId; name = $d.FriendlyName; media = [int]$d.MediaType; bus = [int]$d.BusType
        health = [int]$d.HealthStatus; op = [string]($d.OperationalStatus -join ', ')
        temp = $r.Temperature; tempMax = $r.TemperatureMax; wear = $r.Wear; hours = $r.PowerOnHours
        rErr = $r.ReadErrorsTotal; wErr = $r.WriteErrorsTotal; rUnc = $r.ReadErrorsUncorrected; wUnc = $r.WriteErrorsUncorrected
        starts = $r.StartStopCycleCount; loadCycles = $r.LoadUnloadCycleCount
    }
})
$json = ConvertTo-Json -InputObject $out -Depth 3 -Compress
"""


def _health_script(out_file=None):
    if out_file:   # version admin : écrit le résultat dans un fichier
        return HEALTH_PS + "\n[IO.File]::WriteAllText('%s', $json, [Text.Encoding]::UTF8)\n" % out_file.replace("'", "''")
    return HEALTH_PS + "\n$json\n"


def run_elevated_ps(cmd, wait=60):
    """Lance une commande PowerShell avec l'accord de l'administrateur (fenêtre UAC). True si elle a réussi."""
    if not IS_WIN:
        return False
    os.makedirs(SCANS_DIR, exist_ok=True)
    done = os.path.join(SCANS_DIR, "elevated_%d.txt" % os.getpid())
    try:
        os.remove(done)
    except OSError:
        pass
    full = ("$ErrorActionPreference='Stop'; $r='fail'; try { %s; $r='ok' } catch { $r='fail' }; "
            "[IO.File]::WriteAllText('%s', $r)" % (cmd, done.replace("'", "''")))
    enc = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    if ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                           "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + enc, None, 0) <= 32:
        return False                         # refusé
    for _ in range(wait * 2):
        time.sleep(0.5)
        if os.path.exists(done):
            time.sleep(0.2)
            try:
                with open(done, encoding="utf-8-sig") as f:
                    res = f.read().strip()
                os.remove(done)
            except OSError:
                res = ""
            return res == "ok"
    return False


def disk_health(refresh=False, admin=False):
    """Retourne {date, admin, disks: {id: {...}}} ; le cache évite de relire à chaque ouverture."""
    cache = {}
    try:
        with open(HEALTH_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    if not (refresh or admin):
        return cache or {"disks": {}}       # à l'ouverture : seulement ce qui a déjà été lu
    if not IS_WIN:
        return cache or {"disks": {}}
    rows = None
    if admin:
        tmp = os.path.join(SCANS_DIR, "health_admin.json")
        try:
            os.remove(tmp)
        except OSError:
            pass
        enc = base64.b64encode(_health_script(tmp).encode("utf-16-le")).decode("ascii")
        ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                                 "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + enc,
                                                 None, 0)
        if ok <= 32:      # refusé (UAC) ou impossible
            cache["admin_refused"] = True
            return cache
        for _ in range(120):
            time.sleep(0.5)
            if os.path.exists(tmp):
                time.sleep(0.3)
                try:
                    with open(tmp, encoding="utf-8-sig") as f:
                        rows = json.load(f)
                except Exception:
                    rows = None
                break
    else:
        rows = run_ps_json(_health_script(), timeout=60)
    if rows is None:
        return cache or {"disks": {}}
    if isinstance(rows, dict):
        rows = [rows]
    old = cache.get("disks", {})
    disks = {}
    for r in rows:
        did = str(r.get("id"))
        prev = old.get(did, {})
        # un relevé sans droits admin ne doit pas effacer les valeurs lues avec
        disks[did] = {k: (v if v is not None else prev.get(k)) for k, v in r.items()}
    has_detail = any(d.get("hours") is not None or d.get("temp") is not None for d in disks.values())
    res = {"date": time.time(), "disks": disks, "detail": has_detail or cache.get("detail", False),
           "admin": admin or cache.get("admin", False)}
    try:
        os.makedirs(SCANS_DIR, exist_ok=True)
        with open(HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump(res, f)
    except Exception:
        log.exception("Disk health: cache")
    return res


# --------------------------------------------------------------------------
# Raccourci clavier global pour afficher / masquer le widget
# --------------------------------------------------------------------------
class Hotkey(threading.Thread):
    WM_HOTKEY, WM_APP = 0x0312, 0x8000

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.tid = None
        self.want = None              # (mods, vk) à enregistrer
        self.result = threading.Event()
        self.ok = False

    def run(self):
        if not IS_WIN:
            return
        import ctypes.wintypes as wt
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        self.tid = k32.GetCurrentThreadId()
        msg = wt.MSG()
        u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # crée la file de messages
        registered = False
        self.result.set()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    log.exception("Hotkey")
            elif msg.message == self.WM_APP:
                if registered:
                    u32.UnregisterHotKey(None, 1)
                    registered = False
                if self.want:
                    mods, vk = self.want
                    registered = bool(u32.RegisterHotKey(None, 1, mods | 0x4000, vk))   # MOD_NOREPEAT
                self.ok = registered or not self.want
                self.result.set()

    def set(self, mods, vk):
        """Retourne True si le raccourci a pu être enregistré (sinon il est déjà pris)."""
        if not IS_WIN:
            return False
        if self.tid is None:
            self.result.clear()
            self.start()
            self.result.wait(2)
        self.want = (int(mods), int(vk)) if vk else None
        self.result.clear()
        ctypes.windll.user32.PostThreadMessageW(self.tid, self.WM_APP, 0, 0)
        self.result.wait(2)
        return self.ok


# --------------------------------------------------------------------------
# Réglages (langue, catégories repliées) - enregistrés à côté de l'appli
# --------------------------------------------------------------------------
_settings_lock = threading.RLock()


_WIN_LANG = []


def windows_lang():
    """« fr » si Windows est en français, sinon « en » (lu une seule fois)."""
    if not _WIN_LANG:
        lang = "en"
        try:
            if IS_WIN:
                lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
                lang = "fr" if (lid & 0x3FF) == 0x0C else "en"      # 0x0C = français (France, Belgique, Canada, Suisse…)
            else:
                import locale
                lang = "fr" if (locale.getlocale()[0] or "").lower().startswith("fr") else "en"
        except Exception:
            pass
        _WIN_LANG.append(lang)
    return _WIN_LANG[0]


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            d = d if isinstance(d, dict) else {}
    except Exception:
        d = {}
    if d.get("lang") not in ("fr", "en"):      # pas encore choisie : langue de Windows
        d["lang"] = windows_lang()
    return d


def save_settings(d):
    with _settings_lock:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_FILE)


def update_settings(**kv):
    """Modifie quelques clés sans toucher au reste (utilisé par le widget et le menu de l'icône)."""
    with _settings_lock:
        cur = load_settings()
        cur.update(kv)
        save_settings(cur)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PulseMonitor"


def autostart_get():
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def autostart_set(on):
    """Lancement avec Windows : en fond (icône près de l'horloge), sans ouvrir la fenêtre."""
    if not IS_WIN:
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                cmd = " ".join('"%s"' % a for a in self_cmd()) + " --startup"
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except OSError:
                    pass
    except OSError:
        log.exception("Start with Windows")


THEMES = ("dark", "light", "oled", "midnight", "terminal")


def clean_settings(d):
    def ids(x, n=300):
        return [str(i)[:120] for i in (x if isinstance(x, list) else [])][:n]
    order = d.get("order") if isinstance(d.get("order"), dict) else {}
    manual = d.get("manual") if isinstance(d.get("manual"), dict) else {}
    hidden = d.get("hidden") if isinstance(d.get("hidden"), dict) else {}
    return {
        "lang": d.get("lang") if d.get("lang") in ("fr", "en") else windows_lang(),
        "theme": d.get("theme") if d.get("theme") in THEMES else "dark",
        "collapsed": ids(d.get("collapsed")),
        "order": {str(k)[:40]: ids(v, 100) for k, v in list(order.items())[:20]},
        "manual": {str(k)[:40]: str(v)[:120] for k, v in list(manual.items())[:40] if v not in (None, "")},
        "hidden": {str(k)[:120]: str(v)[:120] for k, v in list(hidden.items())[:100]},
        "big": bool(d.get("big")),
        "tray": d.get("tray") is not False,
        "bg": d.get("bg") is not False,
        "history": d.get("history") is not False,
        "hist_range": d.get("hist_range") if d.get("hist_range") in (1, 6, 12, 24, 168) else 24,
        "hist_compact": d.get("hist_compact") is not False,
        "bell_off": [c for c in (d.get("bell_off") if isinstance(d.get("bell_off"), list) else []) if c in ("cpu", "ram", "gpu", "disk", "startup", "play", "update")],
        "dismissed": {str(k)[:60]: v for k, v in list((d.get("dismissed") if isinstance(d.get("dismissed"), dict) else {}).items())[:50]
                      if isinstance(v, (int, float))},
        # widget : seulement si la page les envoie (le widget gère aussi sa position / épinglage lui-même)
        **({"widget": bool(d["widget"])} if "widget" in d else {}),
        **({"widget_style": d["widget_style"] if d["widget_style"] in ("compact", "bars") else "compact"} if "widget_style" in d else {}),
        **({"widget_alpha": max(25, min(100, int(d["widget_alpha"])))} if isinstance(d.get("widget_alpha"), (int, float)) else {}),
        **({"widget_scale": max(70, min(200, int(d["widget_scale"])))} if isinstance(d.get("widget_scale"), (int, float)) else {}),
    }


# --------------------------------------------------------------------------
# Serveur web local
# --------------------------------------------------------------------------
DISKSCAN = DiskScan()
HOTKEY = [None]


def make_handler(sampler, details):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _cross_site(self):
            # une page web extérieure ne doit pas pouvoir déclencher d'actions sur l'appli
            return self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")

        def _q(self):
            from urllib.parse import parse_qs, urlparse
            return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

        def do_GET(self):
            path = self.path.split("?")[0]
            if path.startswith("/api/") and path not in ("/api/show",) and self._cross_site():
                return self._send(403, b"{}", "application/json")
            if path.startswith("/api/storage/"):
                q, act = self._q(), path[len("/api/storage/"):]
                data = {}
                if act == "drives":
                    data = {"drives": DISKSCAN.drives(), "status": DISKSCAN.status}
                elif act == "scan":
                    data = {"started": DISKSCAN.start(q.get("root", ""))}
                elif act == "stop":
                    DISKSCAN.stop = True
                elif act == "status":
                    data = DISKSCAN.status
                elif act == "summary":
                    data = DISKSCAN.summary(q.get("root", "")) or {}
                elif act == "node":
                    try:
                        parts = json.loads(q.get("path", "[]"))
                    except ValueError:
                        parts = []
                    data = DISKSCAN.node(q.get("root", ""), parts if isinstance(parts, list) else []) or {}
                elif act == "open":
                    data = {"ok": open_in_explorer(q.get("path", ""))}
                elif act == "openfolder":
                    data = {"ok": open_folder(q.get("path", ""))}
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/history/at":
                try:
                    t = int(float(self._q().get("t", 0)))
                except ValueError:
                    t = 0
                return self._send(200, json.dumps({"point": history_at(t) if t else None}).encode(), "application/json")
            if path == "/api/history":
                q = self._q()
                try:
                    vh = max(1, min(24 * 8, int(q.get("view", q.get("hours", 24)))))
                    before = max(1, min(24 * 9, float(q.get("before", vh))))
                    after = max(0, min(24 * 9, float(q.get("after", 0))))
                except ValueError:
                    vh, before, after = 24, 24, 0
                try:
                    end = float(q["end"]) if q.get("end") else None
                except ValueError:
                    end = None
                pts, step, view = read_history(vh, end, q.get("compact") == "1", before, after)
                live = HIST_LIVE[0].live_point() if HIST_LIVE[0] and step == 60 and end is None else None
                if live and (not pts or live["t"] > pts[-1]["t"]):
                    pts.append(live)
                return self._send(200, json.dumps({"points": pts, "step": step, "on": HIST_ON[0], **view}).encode(), "application/json")
            if path.startswith("/api/games/"):
                q, act = self._q(), path[len("/api/games/"):]
                if act == "scan":
                    GAMELIB.scan()
                elif act == "add":
                    GAMELIB.add(q.get("name", ""))
                elif act == "remove":
                    GAMELIB.remove(q.get("name", ""))
                elif act in ("stop", "resume"):
                    key = (q.get("key") or "").lower() or None
                    st = None
                    if GAMES[0] and act == "stop":
                        st = GAMES[0].stop(key)
                    elif GAMES[0] and key:
                        GAMES[0].resume(key)
                    data = GAMES[0].data() if GAMES[0] else {}
                    data["stopped"] = st
                    return self._send(200, json.dumps(data).encode(), "application/json")
                elif act == "delete":
                    try:
                        if GAMES[0]:
                            GAMES[0].delete(int(q.get("start", 0)))
                    except ValueError:
                        pass
                    return self._send(200, json.dumps(GAMES[0].data() if GAMES[0] else {}).encode(), "application/json")
                elif act == "session":
                    try:
                        st = int(q.get("start", 0))
                    except ValueError:
                        st = 0
                    return self._send(200, json.dumps(GAMES[0].timeline(st) if GAMES[0] else {"tl": []}).encode(), "application/json")
                data = GAMELIB.data()
                if act == "apps":
                    data = {"apps": windowed_apps()}
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/games":
                q = self._q()
                if "on" in q:
                    GAMES_ON[0] = q["on"] == "1"
                    update_settings(games=GAMES_ON[0])
                if q.get("clear") == "1" and GAMES[0]:
                    GAMES[0].clear()
                if "fps" in q:
                    FPS_ON[0] = q["fps"] == "1"
                    update_settings(fps=FPS_ON[0])
                if q.get("fps_grant") == "1":
                    fps_grant()
                    FPS.probe()                   # si l'appli tourne en admin, ça marche tout de suite
                data = GAMES[0].data() if GAMES[0] else {"sessions": [], "current": None, "on": GAMES_ON[0]}
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/temp":
                q = self._q()
                return self._send(200, json.dumps(TEMPSCAN.get(q.get("refresh") == "1")).encode(), "application/json")
            if path.startswith("/api/update/"):
                act = path[len("/api/update/"):]
                if act == "check":
                    data = update_check()
                    if not data.get("error"):
                        UPDATE_AVAIL[0] = data.get("latest") if data.get("newer") else None
                elif act == "repo":              # ouvre la page GitHub de l'appli dans le navigateur (seule adresse autorisée)
                    data = {"ok": bool(UPDATE_REPO)}
                    if UPDATE_REPO:
                        webbrowser.open("https://github.com/" + UPDATE_REPO)
                elif act == "auto":
                    on = self._q().get("on") == "1"
                    update_settings(auto_update=on, **({} if on else {"update_last": 0}))
                    data = {"auto_update": on}
                elif act == "install":
                    update_install()
                    data = dict(UPDATE_STATE)
                else:
                    data = dict(UPDATE_STATE)
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/health":
                q = self._q()
                try:
                    data = disk_health(q.get("refresh") == "1", q.get("admin") == "1")
                except Exception:
                    log.exception("Disk health")
                    data = {"disks": {}, "error": True}
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/hotkey":
                q = self._q()
                try:
                    mods, vk = int(q.get("mods", 0)), int(q.get("vk", 0))
                except ValueError:
                    mods, vk = 0, 0
                ok = HOTKEY[0].set(mods, vk) if HOTKEY[0] else False
                if ok or not vk:
                    update_settings(hotkey={"mods": mods, "vk": vk, "label": q.get("label", "")[:40]} if vk else None)
                return self._send(200, json.dumps({"ok": ok or not vk}).encode(), "application/json")
            if path == "/api/live":
                was_idle = ui_idle()
                LAST_PING[0] = time.time()
                if was_idle:  # réveil : mesure immédiate
                    try:
                        with sampler.lock:
                            sampler.latest = sampler.sample()
                    except Exception:
                        log.exception("Measurement after wake-up")
                with sampler.lock:
                    body = json.dumps(dict(sampler.latest, widget_on=WIDGET_ON[0])).encode()
                return self._send(200, body, "application/json")
            if path == "/api/details":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                kind = (q.get("kind") or ["cpu"])[0]
                luid = (q.get("gpu") or [""])[0].lower() or None
                try:
                    data = details.compute(kind if kind in ("cpu", "ram", "disk", "gpu") else "cpu", luid)
                except Exception:
                    log.exception("Details")
                    data = {"kind": kind, "items": [], "error": True}
                return self._send(200, json.dumps(data).encode(), "application/json")
            if path == "/api/show":
                if SHOW_HOOK[0]:
                    threading.Thread(target=SHOW_HOOK[0], daemon=True).start()
                return self._send(200, json.dumps({"app": APP_NAME}).encode(), "application/json")
            if path == "/api/insights_reset":
                if getattr(sampler, "insights", None):
                    sampler.insights.reset_stats()
                return self._send(200, b"{}", "application/json")
            if path == "/api/trim":
                n = trim_memory()
                return self._send(200, json.dumps({"trimmed": n}).encode(), "application/json")
            if path == "/api/specs":
                return self._send(200, json.dumps(sampler.specs).encode(), "application/json")
            if path == "/api/settings":
                st = load_settings()
                st["autostart"] = autostart_get()     # la vérité est dans le registre de Windows
                return self._send(200, json.dumps(st).encode(), "application/json")
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    html = f.read().replace(b"{{VERSION}}", APP_VERSION.encode())
                return self._send(200, html, "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.split("?")[0] != "/api/settings" or self._cross_site():
                return self._send(404, b"not found", "text/plain")
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 20000)
                d = json.loads(self.rfile.read(n).decode("utf-8"))
                if isinstance(d, dict):
                    if "autostart" in d:
                        autostart_set(bool(d["autostart"]))
                    cs = clean_settings(d)
                    with _settings_lock:
                        cur = load_settings()     # garde ce que la page ne gère pas (position du widget…)
                        cur.update(cs)
                        save_settings(cur)
                    BG[0] = cs["bg"]
                    HIST_ON[0] = cs["history"]
                    if ("widget" in cs or "widget_style" in cs or "widget_alpha" in cs or "widget_scale" in cs) and WIDGET_HOOK[0]:
                        WIDGET_HOOK[0]()
                self._send(200, b"{}", "application/json")
            except Exception:
                log.exception("Settings")
                self._send(400, b"{}", "application/json")
    return Handler


def start_server(sampler):
    handler = make_handler(sampler, sampler.details)
    # Sous Windows, « reuse address » laisserait 2 instances partager le même port.
    ThreadingHTTPServer.allow_reuse_address = not IS_WIN
    srv = None
    for port in (8765, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d/" % srv.server_address[1]


def find_app_browser():
    if not IS_WIN:
        return None
    cands = [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    return next((os.path.expandvars(c) for c in cands if os.path.isfile(os.path.expandvars(c))), None)


# --------------------------------------------------------------------------
# Fenêtre (dans son propre processus : la fermer libère toute sa mémoire)
# --------------------------------------------------------------------------
def run_window(url):
    """Processus fenêtre : uniquement pywebview + WebView2."""
    if "--full-gpu" not in sys.argv:   # en cas de souci d'affichage : lancer avec --full-gpu
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", " ".join(LEAN_BROWSER_ARGS))
    import webview
    if IS_WIN:
        try:            # regroupe la fenêtre sous le logo de Pulse Monitor dans la barre des tâches
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PulseMonitor.App")
        except Exception:
            pass
    st = load_settings()
    geo = st.get("window") if isinstance(st.get("window"), dict) else {}
    w, h = geo.get("w", 1320), geo.get("h", 860)
    x, y = geo.get("x"), geo.get("y")
    if not (isinstance(w, int) and isinstance(h, int) and 900 <= w <= 10000 and 600 <= h <= 10000):
        w, h = 1320, 860
    if not (isinstance(x, int) and isinstance(y, int) and _on_screen(x + 40, y + 20)):
        x = y = None          # écran débranché / position invalide : on laisse Windows centrer
    state = {"x": x, "y": y, "w": w, "h": h, "max": bool(geo.get("max"))}
    kw = dict(width=w, height=h, min_size=(900, 600), background_color="#0d0f14")
    if x is not None:
        kw.update(x=x, y=y)
    try:
        win = webview.create_window("%s v%s" % (APP_NAME, APP_VERSION), url, maximized=state["max"], **kw)
    except TypeError:     # ancienne version de pywebview
        win = webview.create_window("%s v%s" % (APP_NAME, APP_VERSION), url, **kw)

    # on suit la position / taille, et on les enregistre à la fermeture
    def moved(*a):
        if len(a) >= 2 and not state["max"]:
            state["x"], state["y"] = int(a[0]), int(a[1])

    def resized(*a):
        if len(a) >= 2 and not state["max"]:
            state["w"], state["h"] = int(a[0]), int(a[1])

    def set_max(v):
        state["max"] = v

    def closing(*_):
        try:
            if not state["max"]:
                for k, attr in (("x", "x"), ("y", "y"), ("w", "width"), ("h", "height")):
                    v = getattr(win, attr, None)
                    if isinstance(v, (int, float)):
                        state[k] = int(v)
            update_settings(window=state)
        except Exception:
            log.exception("Window position")

    def shown(*_):
        set_window_icon(APP_NAME)

    ev = getattr(win, "events", None)
    for name, fn in (("shown", shown), ("moved", moved), ("resized", resized), ("closing", closing),
                     ("maximized", lambda *_: set_max(True)), ("restored", lambda *_: set_max(False))):
        try:
            getattr(ev, name).__iadd__(fn) if ev else None
        except Exception:
            pass
    webview.start()


def _on_screen(x, y):
    """Le point (x, y) est-il sur un écran branché ?"""
    if not IS_WIN:
        return True
    try:
        class PT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        f = ctypes.windll.user32.MonitorFromPoint
        f.restype = ctypes.c_void_p
        f.argtypes = [PT, ctypes.c_uint32]
        return bool(f(PT(x, y), 0))   # MONITOR_DEFAULTTONULL
    except Exception:
        return True


def _has_module(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _gui_python():
    """pythonw.exe si possible (pas de console noire)."""
    exe = sys.executable
    if IS_WIN and os.path.basename(exe).lower() == "python.exe":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(w):
            return w
    return exe


def self_cmd(*args):
    """Commande pour relancer Pulse Monitor (le .exe, ou pythonw + le script)."""
    if FROZEN:
        return [sys.executable, *args]
    return [_gui_python(), os.path.abspath(__file__), *args]


def set_window_icon(title_start):
    """Met le logo sur la fenêtre et dans la barre des tâches (pywebview ne le fait pas sous Windows)."""
    if not IS_WIN or not os.path.isfile(ICON_ICO):
        return
    try:
        u32 = ctypes.WinDLL("user32")
        u32.LoadImageW.restype = ctypes.c_void_p
        u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        big = u32.LoadImageW(None, ICON_ICO, 1, 32, 32, 0x10)      # IMAGE_ICON, LR_LOADFROMFILE
        small = u32.LoadImageW(None, ICON_ICO, 1, 16, 16, 0x10)
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
            pid = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
            if buf.value.startswith(title_start) and pid.value == os.getpid():
                found.append(hwnd)
            return True
        u32.EnumWindows(WNDENUMPROC(cb), 0)
        for hwnd in found:
            u32.SendMessageW(hwnd, 0x0080, ctypes.c_void_p(1), big)      # WM_SETICON, ICON_BIG
            u32.SendMessageW(hwnd, 0x0080, ctypes.c_void_p(0), small)    # ICON_SMALL
    except Exception:
        log.exception("Window icon")


def _focus_window(title_start):
    if not IS_WIN:
        return
    try:
        u32 = ctypes.WinDLL("user32")
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            u32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value.startswith(title_start) and u32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                found.append(hwnd)
            return True
        u32.EnumWindows(WNDENUMPROC(cb), 0)
        for hwnd in found:
            u32.ShowWindow(ctypes.c_void_p(hwnd), 9)          # SW_RESTORE
            u32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:
        log.exception("Bring to front")


class WindowManager:
    def __init__(self, url, on_closed):
        self.url, self.on_closed = url, on_closed
        self.proc = None
        self.lock = threading.Lock()
        self.native = _has_module("webview")

    def is_open(self):
        return self.proc is not None and self.proc.poll() is None

    def pid(self):
        return self.proc.pid if self.is_open() else None

    def open(self):
        with self.lock:
            if self.is_open():
                _focus_window(APP_NAME)
                return
            if self.native:
                args = self_cmd("--window", self.url)
                if "--full-gpu" in sys.argv:
                    args.append("--full-gpu")
                self.proc = subprocess.Popen(args, creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
                threading.Thread(target=self._watch, args=(self.proc,), daemon=True).start()
            else:
                exe = find_app_browser()
                if exe:
                    subprocess.Popen([exe, "--app=" + self.url, "--window-size=1320,860"] + LEAN_BROWSER_ARGS)
                else:
                    webbrowser.open(self.url)

    def _watch(self, proc):
        code = proc.wait()
        (log.info if code == 0 else log.warning)("Window closed (code %s%s)", code, "" if code == 0 else ": abnormal exit")
        self.on_closed()

    def close(self):
        if self.is_open():
            try:
                self.proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Widget de bureau (fenêtre native Tk : quelques Mo, pas de navigateur)
# --------------------------------------------------------------------------
class DesktopWidget:
    BG, FG, MUTED, TRACK = "#11151d", "#e8ebf2", "#8b93a7", "#262c3b"
    COLORS = {"cpu": "#4cc2ff", "gpu": "#ffa94d", "vram": "#2fd6c8", "ram": "#b38cff"}
    STYLES = ("compact", "bars")
    WARN, BAD = "#ffb547", "#ff5d6c"
    KEY = "#010203"   # couleur rendue invisible (et traversée par les clics) par Windows

    def __init__(self, sampler, kmt):
        self.s, self.kmt = sampler, kmt
        self.thread = None
        self.cmds = []
        self.lock = threading.Lock()
        self.root = None
        self.prev_cpu = None
        self.ctrl_down = False
        self.on_change = lambda: None   # pour rafraîchir le menu de l'icône

    # ---------- appelé depuis n'importe quel thread ----------
    def _post(self, *cmd):
        with self.lock:
            self.cmds.append(cmd)
        if self.thread is None:
            if not _has_module("tkinter"):
                log.error("Widget: tkinter is missing from this Python installation")
                return
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def apply_settings(self):
        st = load_settings()
        self._post("style", st.get("widget_style") if st.get("widget_style") in self.STYLES else "compact")
        self._post("alpha", self._alpha(st))
        self._post("scale", self._scale(st))
        self._post("show", bool(st.get("widget")))

    @staticmethod
    def _scale(st):
        v = st.get("widget_scale")
        return max(70, min(200, v)) / 100 if isinstance(v, (int, float)) else 1.0

    @staticmethod
    def _alpha(st):
        a = st.get("widget_alpha")
        return max(25, min(100, a)) / 100 if isinstance(a, (int, float)) else 0.86

    def set_visible(self, on):
        log.info("Widget: %s (close button, tray menu or hotkey)", "shown" if on else "hidden")
        update_settings(widget=bool(on))
        self._post("show", bool(on))

    def set_pinned(self, on):
        update_settings(widget_pinned=bool(on))
        self._post("pin", bool(on))
        if on:
            update_settings(widget=True)
            self._post("show", True)

    def pinned(self):
        return bool(getattr(self, "is_pinned", load_settings().get("widget_pinned")))

    # ---------- thread Tk ----------
    def _run(self):
        import tkinter as tk
        self.tk = tk
        st = load_settings()
        self.lang = st.get("lang", "fr")
        self.style = st.get("widget_style", "bars") if st.get("widget_style") in self.STYLES else "compact"
        self.is_pinned = bool(st.get("widget_pinned"))
        self.visible = False
        root = self.root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)            # sans bordure ni bouton dans la barre des tâches
        root.configure(bg=self.KEY)
        root.attributes("-alpha", self._alpha(st))
        if IS_WIN:
            root.attributes("-transparentcolor", self.KEY)
        self.pt = root.winfo_fpixels("1i") / 72             # pixels par point (écran)
        self.k = self._scale(st)
        self._make_ui()
        x, y = st.get("widget_x"), st.get("widget_y")         # position des chiffres
        if not isinstance(x, int) or not isinstance(y, int):
            x, y = root.winfo_screenwidth() - 260, 70
        root.geometry("+%d+%d" % (x - self.side_w, y))
        root.bind("<ButtonPress-1>", self._drag_start)
        root.bind("<B1-Motion>", self._drag_move)
        root.bind("<ButtonRelease-1>", self._drag_end)
        root.after(100, self._loop_cmds)
        root.after(300, self._update)
        root.after(200, self._watch_ctrl)
        log.info("Widget: started (pinned=%s, position=%s,%s)", self.is_pinned, x, y)
        try:
            root.mainloop()
        except Exception:
            log.exception("Widget: unexpected stop")
        log.warning("Widget: Tk loop ended")

    def S(self, v):
        """Taille mise à l'échelle choisie dans les Paramètres."""
        return max(1, round(v * self.k))

    def _make_ui(self):
        # Fenêtre de taille FIXE : [zone des boutons | chiffres]. La zone des boutons est toujours là ;
        # vide, elle est de la couleur invisible. Afficher/masquer les boutons ne redimensionne et ne déplace
        # donc jamais la fenêtre (plus de clignotement ni d'élargissement).
        tk = self.tk
        fpx = max(8, round(9 * self.pt * self.k))
        self.font = ("Segoe UI", -fpx)
        self.bold = ("Segoe UI Semibold", -fpx)
        self.ibox, self.bw, self.bh = self.S(20), self.S(56), self.S(6)
        self._build_header()
        self.outer = tk.Frame(self.root, bg=self.BG, padx=0, pady=self.S(6))
        self.outer.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.outer, bg=self.BG)
        self.body.pack(fill="both", expand=True)
        self._build_body()

    def _rescale(self, k):
        if abs(k - self.k) < 0.001:
            return
        root = self.root
        x, y = root.winfo_x() + self.side_w, root.winfo_y()   # position des chiffres : elle ne bouge pas
        self._hide_tip()
        self.side.destroy()
        self.outer.destroy()
        self.k = k
        self._make_ui()
        root.geometry("+%d+%d" % (x - self.side_w, y))
        self._lastw = 0
        if self.visible:
            root.after(60, self._fit)

    def _work_area(self, px, py):
        """Zone de travail (sans la barre des tâches) de l'écran qui contient ce point."""
        L, T, R, B = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if IS_WIN:
            try:
                from ctypes import wintypes

                class MI(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
                u = ctypes.windll.user32
                mon = u.MonitorFromPoint(wintypes.POINT(int(px), int(py)), 2)
                mi = MI(); mi.cbSize = ctypes.sizeof(MI)
                if u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    L, T, R, B = mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom
            except Exception:
                pass
        return L, T, R, B

    def _fit(self):
        """Garde le widget entier à l'écran (le style barres est plus large : sinon la RAM sortait de l'écran)."""
        try:
            root = self.root
            root.update_idletasks()
            sw = self.side_w
            w, h = root.winfo_reqwidth() - sw, root.winfo_reqheight()    # partie visible : les chiffres
            x, y = root.winfo_x() + sw, root.winfo_y()
            L, T, R, B = self._work_area(x + min(w, 40), y + 10)
            nx, ny = max(L, min(x, R - w)), max(T, min(y, B - h))
            if (nx, ny) != (x, y):
                root.geometry("+%d+%d" % (nx - sw, ny))
                update_settings(widget_x=nx, widget_y=ny)
        except Exception:
            log.exception("Widget: placement")

    # icônes : chacune centrée dans une case de taille fixe (les symboles n'ont pas tous la même ligne de base)
    ICONS = {"close": ("✕", 0, 0), "pin": ("📌", 0, 0), "snap": ("🧲", -2, 0), "style": ("◫", -1, 4)}   # (symbole, décalage vertical px, px en plus)
    TIPS = {
        "fr": {"close": "Masquer le widget", "pin": "Épingler au-dessus de tout (clics à travers)", "unpin": "Désépingler",
               "snap": "Aimanter aux bords de l'écran : activé", "nosnap": "Aimanter aux bords de l'écran : désactivé",
               "style": "Changer l'apparence (compact / barres)"},
        "en": {"close": "Hide the widget", "pin": "Pin on top of everything (click-through)", "unpin": "Unpin",
               "snap": "Snap to screen edges: on", "nosnap": "Snap to screen edges: off",
               "style": "Change the look (compact / bars)"},
    }

    def _paint(self, b, color):
        b.lbl.configure(fg=color)

    def _tip_text(self, kind):
        t = self.TIPS.get(self.lang, self.TIPS["en"])
        if kind == "pin":
            return t["unpin"] if self.is_pinned else t["pin"]
        if kind == "snap":
            return t["snap"] if self.snap else t["nosnap"]
        return t[kind]

    def _show_tip(self, b, kind):
        self._hide_tip()
        tk = self.tk
        tw = self._tip = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=self._tip_text(kind), bg="#1b2130", fg=self.FG, font=self.font, padx=6, pady=2,
                 highlightthickness=1, highlightbackground="#333b4d").pack()
        tw.update_idletasks()
        x = b.winfo_rootx() + b.winfo_width() // 2 - tw.winfo_reqwidth() // 2
        y = b.winfo_rooty() - tw.winfo_reqheight() - 6
        if y < 0:
            y = b.winfo_rooty() + b.winfo_height() + 6
        tw.geometry("+%d+%d" % (max(0, x), y))

    def _hide_tip(self):
        if getattr(self, "_tip_job", None):
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        if getattr(self, "_tip", None):
            self._tip.destroy()
            self._tip = None

    def _icon_btn(self, parent, kind, cb):
        tk, n = self.tk, self.ibox
        text, dy, grow = self.ICONS[kind]
        px = round(9 * self.pt * self.k) + round(grow * self.k)       # 9 pt en pixels (à l'échelle), + agrandissement
        dy = round(dy * self.k)
        b = tk.Frame(parent, bg=self.BG, width=n, height=n, cursor="hand2")
        b.pack_propagate(False)
        b.lbl = tk.Label(b, text=text, bg=self.BG, fg=self.MUTED, font=("Segoe UI Symbol", -px), bd=0, padx=0, pady=0, cursor="hand2")
        b.lbl.place(relx=0.5, rely=0.5, y=dy, anchor="center")
        rest = lambda: self.COLORS["cpu"] if ((kind == "pin" and self.is_pinned) or (kind == "snap" and self.snap)) else self.MUTED

        def enter(e):
            self._paint(b, self.FG)
            self._hide_tip()
            self._tip_job = self.root.after(450, lambda: self._show_tip(b, kind))

        def leave(e):
            if e.widget is b and b.winfo_containing(e.x_root, e.y_root) in (b, b.lbl):
                return
            self._paint(b, rest())
            self._hide_tip()

        def click(e):
            self._hide_tip()
            cb()
            return "break"
        for w in (b, b.lbl):
            w.bind("<Enter>", enter)
            w.bind("<Leave>", leave)
            w.bind("<ButtonRelease-1>", click)
        return b

    def _build_header(self):
        """Boutons à GAUCHE des chiffres : ✕ 📌 🧲 ◫ quand le widget est libre, 📌 🧲 ◫ quand il est épinglé
        et que Ctrl est maintenu. Ils sont dans une zone de largeur fixe : les chiffres ne bougent jamais."""
        tk = self.tk
        self.side = tk.Frame(self.root, bg=self.KEY)          # zone fixe, invisible là où il n'y a rien
        self.pad = tk.Frame(self.side, bg=self.BG, width=self.S(10))  # marge gauche du cadre, toujours visible
        self.pad.pack(side="right", fill="y")
        self.strip = tk.Frame(self.side, bg=self.BG, padx=self.S(4))  # fond juste derrière les boutons affichés
        self.close_btn = self._icon_btn(self.strip, "close", lambda: self.set_visible(False))
        self.pin_btn = self._icon_btn(self.strip, "pin", lambda: self.set_pinned(not self.is_pinned))
        self.style_btn = self._icon_btn(self.strip, "style", self._toggle_style)
        self.snap_btn = self._icon_btn(self.strip, "snap", self._toggle_snap)
        self.snap = load_settings().get("widget_snap", True) is not False
        self.root.update_idletasks()
        sets = ([self.close_btn, self.pin_btn, self.snap_btn, self.style_btn], [self.pin_btn, self.snap_btn, self.style_btn])
        # marge gauche du cadre (10 px) toujours visible : elle fait partie de la zone des boutons
        self.side_w = max(sum(self.ibox + 2 for w in ws) for ws in sets) + self.S(4) * 2 + self.S(10)
        self.side.configure(width=self.side_w)
        self.side.pack_propagate(False)
        self.side.pack(side="left", fill="y")
        self._shown = None

    def _toggle_style(self):
        i = self.STYLES.index(self.style) if self.style in self.STYLES else 0
        self.style = self.STYLES[(i + 1) % len(self.STYLES)]
        update_settings(widget_style=self.style)
        self._build_body()
        self._fit()

    def _build_body(self):
        """Tout sur une ligne : compact = textes, barres = petite barre colorée par mesure."""
        tk = self.tk
        for w in self.body.winfo_children():
            w.destroy()
        self.cells = {}
        row = tk.Frame(self.body, bg=self.BG)
        row.pack(fill="x", pady=(1, 0))
        for i, (k, name) in enumerate([("cpu", "CPU"), ("gpu", "GPU"), ("vram", "VRAM"), ("ram", "RAM")]):
            if i:
                tk.Label(row, text="·" if self.style == "compact" else " ", bg=self.BG, fg=self.MUTED, font=self.font).pack(side="left", padx=self.S(4))
            tk.Label(row, text=name, bg=self.BG, fg=self.COLORS[k], font=self.bold).pack(side="left", padx=(0, self.S(5)))
            cell = {}
            if self.style == "bars":
                c = tk.Canvas(row, width=self.bw, height=self.bh, bg=self.TRACK, highlightthickness=0, bd=0)
                c.pack(side="left", padx=(0, self.S(5)))
                cell.update(canvas=c, bar=c.create_rectangle(0, 0, 0, self.bh, fill=self.COLORS[k], width=0))
            cell["val"] = tk.Label(row, text="–", bg=self.BG, fg=self.FG, font=self.font)
            cell["val"].pack(side="left")
            self.cells[k] = cell
        # session de jeu en cours : petit texte vert au bout de la ligne (visible même épinglé)
        # en jeu seulement : le point vert (remonté de 1 px pour être centré sur le texte) puis les FPS
        self.game_box = tk.Frame(row, bg=self.BG)
        self.fps_lbl = tk.Label(self.game_box, text="", bg=self.BG, fg=self.FG, font=self.bold)
        self.game_lbl = tk.Label(self.game_box, text="●", bg=self.BG, fg="#5fd38d", font=self.bold)
        self.game_lbl.pack(side="left", pady=(0, self.S(2)))
        self._game_on = False
        tk.Frame(row, bg=self.BG, width=self.S(10)).pack(side="right")                             # marge droite du cadre
        self._apply_pin_look()

    # ---------- commandes (depuis les autres threads) ----------
    def _loop_cmds(self):
        with self.lock:
            cmds, self.cmds = self.cmds, []
        changed = False
        for cmd in cmds:
            if cmd[0] in ("show", "pin"):
                changed = True
            if cmd[0] == "show":
                if cmd[1] != self.visible:
                    log.info("Widget: %s", "shown" if cmd[1] else "hidden")
                self.visible = cmd[1]
                WIDGET_ON[0] = cmd[1]
                if cmd[1]:
                    self.root.deiconify()
                    self._apply_pin()
                    self.root.after(60, self._fit)
                else:
                    self.root.withdraw()
            elif cmd[0] == "pin":
                log.info("Widget: %s", "pinned" if cmd[1] else "unpinned")
                self.is_pinned = cmd[1]
                self._apply_pin()
            elif cmd[0] == "alpha":
                self.root.attributes("-alpha", cmd[1])
            elif cmd[0] == "scale":
                self._rescale(cmd[1])
            elif cmd[0] == "style" and cmd[1] != self.style:
                self.style = cmd[1]
                self._build_body()
                if self.visible:
                    self._fit()
        if changed:
            # le menu de l'icône est rafraîchi une fois l'état réellement appliqué
            threading.Thread(target=self.on_change, daemon=True).start()
        self.root.after(150, self._loop_cmds)

    # ---------- épinglage : au-dessus de tout + clics à travers (Ctrl maintenu = interaction) ----------
    def _hwnd(self):
        return ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)   # GA_ROOT

    def _set_clickthrough(self, on):
        if not IS_WIN:
            return
        try:
            u32 = ctypes.windll.user32
            h = self._hwnd()
            ex = u32.GetWindowLongW(h, -20)
            ex = (ex | 0x80000 | 0x20) if on else (ex & ~0x20)     # WS_EX_LAYERED | WS_EX_TRANSPARENT
            ex |= 0x80 | 0x08000000                                  # WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            u32.SetWindowLongW(h, -20, ex)
        except Exception:
            log.exception("Widget: click-through")

    def _set_desktop_owner(self, on):
        """Libre : rattaché au Bureau, il reste visible quand on clique sur « Afficher le Bureau » (Win+D)."""
        if not IS_WIN:
            return
        try:
            u32 = ctypes.windll.user32
            setp = getattr(u32, "SetWindowLongPtrW", u32.SetWindowLongW)
            setp.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            owner = u32.FindWindowW("Progman", None) if on else 0
            setp(ctypes.c_void_p(self._hwnd()), -8, ctypes.c_void_p(owner or 0))     # GWLP_HWNDPARENT = propriétaire
        except Exception:
            log.exception("Widget: attach to desktop")

    def _apply_pin(self):
        self._set_desktop_owner(not self.is_pinned)
        self.root.attributes("-topmost", self.is_pinned)
        self._set_clickthrough(self.is_pinned and not self.ctrl_down)
        self._apply_pin_look()

    def _apply_pin_look(self):
        if not self.root:
            return
        self._paint(self.pin_btn, self.COLORS["cpu"] if self.is_pinned else self.MUTED)
        self._paint(self.snap_btn, self.COLORS["cpu"] if self.snap else self.MUTED)
        if self.is_pinned:
            show = (self.pin_btn, self.snap_btn, self.style_btn) if self.ctrl_down else ()
        else:
            show = (self.close_btn, self.pin_btn, self.snap_btn, self.style_btn)
        if show == self._shown:
            return
        self._shown = show
        self._hide_tip()
        for w in (self.close_btn, self.pin_btn, self.snap_btn, self.style_btn):
            w.pack_forget()
        for w in reversed(show):                      # collés aux chiffres, dans l'ordre de gauche à droite
            w.pack(side="right", padx=(2, 0))
        if show:
            self.strip.pack(side="right", fill="y")
        else:
            self.strip.pack_forget()

    def _fg_desc(self):
        """Fenêtre au premier plan (programme, plein écran ?) : pour comprendre ce qui a caché le widget."""
        try:
            pid, full = _fg_window()
            name = psutil.Process(pid).name() if pid else "?"
            return "%s%s" % (name, " fullscreen" if full else "")
        except Exception:
            return "?"

    def _keep_alive(self):
        """Épinglé : Windows peut le cacher (Afficher le bureau, jeu plein écran, explorateur relancé…)
        ou le faire passer derrière une autre fenêtre « au-dessus de tout ». On le remet, sans l'activer.
        Chaque problème est noté (une fois) dans pulse_monitor.log."""
        if not (IS_WIN and self.visible):
            return
        try:
            from ctypes import wintypes
            u32, h = ctypes.windll.user32, self._hwnd()
            issues = []
            if not u32.IsWindowVisible(h):
                issues.append("hidden by Windows")
            if u32.IsIconic(h):
                issues.append("minimized")
            rc = wintypes.RECT()
            if u32.GetWindowRect(h, ctypes.byref(rc)):
                c = wintypes.POINT((rc.left + rc.right) // 2 + self.side_w // 2, (rc.top + rc.bottom) // 2)
                if not u32.MonitorFromPoint(c, 0):              # MONITOR_DEFAULTTONULL : hors de tous les écrans
                    issues.append("off screen (%d,%d)" % (rc.left, rc.top))
            if self.is_pinned and not (u32.GetWindowLongW(h, -20) & 0x8):   # WS_EX_TOPMOST perdu
                issues.append("no longer on top")
            key = tuple(issues)
            if key != getattr(self, "_ka_issue", ()):
                if issues:
                    log.warning("Widget: %s - foreground: %s - restored", ", ".join(issues), self._fg_desc())
                elif getattr(self, "_ka_issue", ()):
                    log.info("Widget: visible again")
                self._ka_issue = key
            if "hidden by Windows" in issues or "minimized" in issues:
                u32.ShowWindow(h, 4)                                   # SW_SHOWNOACTIVATE
            if any(i.startswith("off screen") for i in issues):
                self._fit()
            if self.is_pinned:
                u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                u32.SetWindowPos(ctypes.c_void_p(h), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)
                # HWND_TOPMOST ; SWP_NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER
        except Exception:
            log.exception("Widget: keep on screen")

    def _watch_ctrl(self):
        self._ka = getattr(self, "_ka", 0) + 1
        if self._ka % 10 == 0:                     # toutes les 2 s
            self._keep_alive()
        if self.visible and self.is_pinned and IS_WIN:
            down = bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)   # VK_CONTROL
            if down != self.ctrl_down:          # Ctrl : on ne change que les clics à travers et les boutons
                self.ctrl_down = down           # (pas le « toujours au-dessus », qui faisait clignoter le widget)
                self._set_clickthrough(not down)
                self._apply_pin_look()
        elif self.ctrl_down:
            self.ctrl_down = False
            self._set_clickthrough(self.is_pinned)
            self._apply_pin_look()
        self.root.after(200, self._watch_ctrl)

    # ---------- déplacement à la souris ----------
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    SNAP = 18   # px : distance à laquelle le widget se colle au bord de l'écran

    def _drag_move(self, e):
        x, y = e.x_root - self._dx, e.y_root - self._dy
        if self.snap:                                  # aimanté : se colle aux bords de l'écran (zone sans la barre des tâches)
            sw = self.side_w                           # les bords comptent à partir des chiffres
            w, h = self.root.winfo_width(), self.root.winfo_height()
            L, T, R, B = self._work_area(e.x_root, e.y_root)
            if abs(x + sw - L) < self.SNAP: x = L - sw
            elif abs(x + w - R) < self.SNAP: x = R - w
            if abs(y - T) < self.SNAP: y = T
            elif abs(y + h - B) < self.SNAP: y = B - h
        self.root.geometry("+%d+%d" % (x, y))

    def _toggle_snap(self):
        self.snap = not self.snap
        update_settings(widget_snap=self.snap)
        self._apply_pin_look()

    def _drag_end(self, e):
        x = self.root.winfo_x() + self.side_w           # position des chiffres (sans la zone des boutons)
        update_settings(widget_x=x, widget_y=self.root.winfo_y())

    # ---------- mesures ----------
    def _measure(self):
        cur = psutil.cpu_times()
        cpu = 0.0
        if self.prev_cpu:
            tot = sum(cur) - sum(self.prev_cpu)
            cpu = max(0.0, 100.0 * (1 - (cur.idle - self.prev_cpu.idle) / tot)) if tot > 0 else 0.0
        self.prev_cpu = cur
        vm = psutil.virtual_memory()
        gpu_use, gpu_mem, _ = self.s.counters.snapshot()
        specs = self.s.specs.get("gpus", []) if isinstance(self.s.specs, dict) else []
        best = None
        for a in self.kmt.adapters:
            spec = next((x for x in specs if x["name"] == a["name"]), {})
            if spec.get("integrated"):
                continue
            u = gpu_use.get(a["luid"])
            if best is None or (u or 0) > (best[1] or 0):
                best = (a, u, spec)
        gpu = temp = vu = vt = None
        if best:
            a, gpu, spec = best
            temp = self.kmt.temperature(a["handle"])
            vu = gpu_mem.get(a["luid"], (None, None))[0]
            vt = (spec.get("vram_gb") or 0) * GB or None
        return {"cpu": cpu, "ram": vm.percent, "ram_used": vm.total - vm.available, "gpu": gpu, "temp": temp, "vu": vu, "vt": vt}

    def _color(self, k, pct):
        if pct is not None and pct >= 90:
            return self.BAD
        if pct is not None and pct >= 75:
            return self.WARN
        return self.COLORS[k]

    def _update(self):
        if self.visible:
            try:
                m = self._measure()
                fr = self.lang == "fr"
                unit = "Go" if fr else "GB"
                fmt = lambda b: ("%.1f" % (b / GB)).replace(".", "," if fr else ".")
                pc = lambda v: "–" if v is None else ("%d %%" % v if fr else "%d%%" % v)
                vr = (m["vu"] / m["vt"] * 100) if m["vu"] and m["vt"] else None
                vals = {
                    "cpu": (m["cpu"], pc(m["cpu"])),
                    "gpu": (m["gpu"], pc(m["gpu"]) + ("  %d°C" % m["temp"] if m["temp"] else "")),
                    "vram": (vr, "%s %s" % (fmt(m["vu"]), unit) if m["vu"] else "–"),
                    "ram": (m["ram"], "%s  %s %s" % (pc(m["ram"]), fmt(m["ram_used"]), unit)),
                }
                g = GAMES[0].curs if GAMES[0] else None
                on = bool(g)
                fps = max((c.get("fps_now") or 0 for c in g.values()), default=0) if g else 0
                ft = "%d FPS" % round(fps) if fps else ""
                if self.fps_lbl.cget("text") != ft:
                    self.fps_lbl.configure(text=ft)
                    if ft:
                        self.fps_lbl.pack(side="left", padx=(self.S(5), 0))
                    else:
                        self.fps_lbl.pack_forget()
                if on != self._game_on:
                    self._game_on = on
                    if on:
                        self.game_box.pack(side="left", padx=(self.S(10), 0))
                    else:
                        self.game_box.pack_forget()
                for k, cell in self.cells.items():
                    pct, txt = vals[k]
                    hot = (pct is not None and pct >= 90) or (k == "gpu" and m["temp"] and m["temp"] >= 85)
                    cell["val"].configure(text=txt, fg=self.BAD if hot else self.FG)
                    if "canvas" in cell:
                        cell["canvas"].coords(cell["bar"], 0, 0, self.bw * min(100, pct or 0) / 100, self.bh)
                        cell["canvas"].itemconfigure(cell["bar"], fill=self._color(k, pct))
                w = self.root.winfo_reqwidth()
                if w != getattr(self, "_lastw", 0):    # la largeur a changé : rester entier à l'écran
                    self._lastw = w
                    self._fit()
            except Exception:
                log.exception("Widget: measurement")
        self.root.after(1500, self._update)


# --------------------------------------------------------------------------
# Icône près de l'horloge (zone de notification)
# --------------------------------------------------------------------------
TRAY_TEXT = {
    "fr": {"open": "Ouvrir Pulse Monitor", "quit": "Quitter", "widget": "Widget de bureau", "pin": "Épingler le widget",
           "hint": "Pulse Monitor continue en arrière-plan. Clique sur l'icône pour le rouvrir."},
    "en": {"open": "Open Pulse Monitor", "quit": "Quit", "widget": "Desktop widget", "pin": "Pin the widget",
           "hint": "Pulse Monitor keeps running in the background. Click the icon to reopen it."},
}


TRAY_ID = 0x50554C53   # identifiant fixe : Windows retient la position de l'icône d'un lancement à l'autre


def stable_tray_id():
    """pystray donne à l'icône un identifiant différent à chaque lancement (id(self)) ;
    Windows la prend alors pour une nouvelle icône et la remet tout à droite. On le fixe."""
    if not IS_WIN:
        return
    try:
        import pystray._win32 as pw
        w32 = pw.win32
        fields = [f[0] for f in w32.NOTIFYICONDATAW._fields_]
        id_field = "hID" if "hID" in fields else "uID" if "uID" in fields else None
        if not id_field:
            return
        original = pw.Icon._message

        def _message(self, code, flags, **kwargs):
            try:
                data = {"cbSize": ctypes.sizeof(w32.NOTIFYICONDATAW), "hWnd": self._hwnd,
                        id_field: TRAY_ID, "uFlags": flags}
                data.update(kwargs)
                return w32.Shell_NotifyIcon(code, w32.NOTIFYICONDATAW(**data))
            except Exception:
                log.exception("Icon: fixed id impossible, using original method")
                return original(self, code, flags, **kwargs)
        pw.Icon._message = _message
    except Exception:
        log.exception("Icon: id patch")


def tray_image():
    from PIL import Image, ImageDraw
    try:
        return Image.open(ICON_PNG).convert("RGBA")
    except Exception:
        pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(27, 32, 48, 255))
    d.line([(9, 35), (20, 35), (26, 18), (36, 48), (42, 31), (55, 31)], fill=(76, 194, 255, 255), width=5, joint="curve")
    return img


class HealthWatch(threading.Thread):
    """Surveille l'appli elle-même, 1 fois par minute (coût négligeable) : CPU, RAM, GPU, nombre de fils
    d'exécution. Note dans le journal ce qui sort de l'ordinaire (une fois par heure au plus pour chaque problème)
    et un bilan toutes les 6 h."""
    CPU_WARN = 5.0          # % du processeur entier, en moyenne sur 3 minutes de suite
    RAM_WARN = 450          # Mo (appli + fenêtre)
    GPU_WARN = 15.0         # % de la carte graphique, 3 minutes de suite
    THREADS_WARN = 90

    def __init__(self, counters):
        super().__init__(daemon=True, name="health")
        self.counters = counters
        self.me = psutil.Process()
        self.ncpu = psutil.cpu_count() or 1
        self.last_warn = {}
        self.high = collections.Counter()
        self.base_py = None
        self.t0 = time.time()
        self.sum = collections.Counter()

    def _warn(self, key, msg, *a):
        if time.time() - self.last_warn.get(key, 0) >= 3600:
            self.last_warn[key] = time.time()
            log.warning(msg, *a)

    def _procs(self):
        try:
            return [self.me] + self.me.children(recursive=True)
        except psutil.Error:
            return [self.me]

    def run(self):
        prev, n = {}, 0
        while True:
            time.sleep(60)
            try:
                now = time.time()
                cpu = ram = 0.0
                cur = {}
                for p in self._procs():
                    try:
                        t = p.cpu_times()
                        cur[p.pid] = (now, t.user + t.system)
                        ram += p.memory_info().rss
                    except psutil.Error:
                        continue
                    old = prev.get(p.pid)
                    if old:
                        cpu += (cur[p.pid][1] - old[1]) / max(1, now - old[0]) / self.ncpu * 100
                prev = cur
                ram_mb = ram / 1048576
                py_mb = self.me.memory_info().rss / 1048576
                gpu = sum(v for pid, v in self.counters.gpu_by_pid(None).items() if pid in cur) if self.counters else 0.0
                threads = self.me.num_threads()
                n += 1
                self.sum.update(cpu=cpu, ram=ram_mb, gpu=gpu)
                for k, v, lim in (("cpu", cpu, self.CPU_WARN), ("gpu", gpu, self.GPU_WARN)):
                    self.high[k] = self.high[k] + 1 if v >= lim else 0
                    if self.high[k] >= 3:
                        self._warn(k, "App health: high %s (%.1f %% for %d min)", k.upper(), v, self.high[k])
                if ram_mb >= self.RAM_WARN:
                    self._warn("ram", "App health: high RAM (%.0f MB, app + window)", ram_mb)
                if n == 10:
                    self.base_py = py_mb                        # référence après le démarrage
                elif self.base_py and py_mb > self.base_py * 1.8 and py_mb - self.base_py > 80:
                    self._warn("leak", "App health: program memory keeps growing (%.0f MB, %.0f MB at start): possible leak", py_mb, self.base_py)
                if threads >= self.THREADS_WARN:
                    self._warn("threads", "App health: many threads (%d): possible leak", threads)
                if n % 360 == 0:                                # bilan toutes les 6 h
                    log.info("App health: running for %.0f h - averages: CPU %.1f %%, RAM %.0f MB, GPU %.1f %%; threads: %d",
                             (now - self.t0) / 3600, self.sum["cpu"] / n, self.sum["ram"] / n, self.sum["gpu"] / n, threads)
            except Exception:
                log.exception("App health")


def main():
    if "--window" in sys.argv:
        return run_window(sys.argv[sys.argv.index("--window") + 1])

    if "--wait-pid" in sys.argv:          # relance après une mise à jour : on attend que l'ancienne version soit fermée
        try:
            psutil.Process(int(sys.argv[sys.argv.index("--wait-pid") + 1])).wait(timeout=15)
        except Exception:
            pass
    # Déjà lancé ? On rouvre simplement sa fenêtre.
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8765/api/show", timeout=1) as r:
            if json.loads(r.read().decode()).get("app") == APP_NAME:
                return
    except Exception:
        pass

    if os.path.exists(RUN_FLAG):
        log.warning("Previous run did not exit normally (crash, forced close, or PC shut down without quitting)")
    try:
        with open(LOG_FILE + ".crash", encoding="utf-8") as f:
            crash = f.read().strip()
        if crash:
            log.critical("Hard crash during the previous run:\n%s", crash[-4000:])
        open(LOG_FILE + ".crash", "w").close()
    except OSError:
        pass
    try:
        with open(RUN_FLAG, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log.info("Starting Pulse Monitor v%s (Python %s, %s)", APP_VERSION, platform.python_version(), platform.platform())

    BG[0] = load_settings().get("bg", True) is not False
    kmt = D3DKMT()
    counters = CounterReader()
    counters.start()
    sampler = Sampler(counters, kmt)
    sampler.start()

    def load_specs():
        try:
            sampler.specs = collect_specs(kmt)
        except Exception:
            log.exception("Could not read components")
            sampler.specs = {"ready": True, "error": True, "tips": []}
    threading.Thread(target=load_specs, daemon=True).start()

    url = start_server(sampler)
    log.info("Server on %s", url)
    HealthWatch(counters).start()

    def quit_app(*_):
        QUIT_HOOK[0] = None
        log.info("Normal exit")
        try:
            os.remove(RUN_FLAG)
        except OSError:
            pass
        try:
            if getattr(sampler, "insights", None):
                sampler.insights.save_stats()
        except Exception:
            pass
        wm.close()
        if counters.proc:
            try:
                counters.proc.kill()
            except Exception:
                pass
        if icon:
            try:
                icon.stop()
            except Exception:
                pass
        os._exit(0)

    QUIT_HOOK[0] = quit_app
    icon = None
    hint_shown = [False]

    def on_window_closed():
        if icon and load_settings().get("tray", True):
            trim_memory()   # il ne reste que Python : on rend le reste à Windows
            if not hint_shown[0]:
                hint_shown[0] = True
                try:
                    icon.notify(TRAY_TEXT[load_settings().get("lang", "fr")]["hint"], APP_NAME)
                except Exception:
                    pass
        else:
            quit_app()

    wm = WindowManager(url, on_window_closed)
    SHOW_HOOK[0] = wm.open
    HIST_ON[0] = load_settings().get("history", True) is not False
    GAMES_ON[0] = load_settings().get("games", True) is not False
    FPS_ON[0] = load_settings().get("fps", True) is not False
    threading.Thread(target=FPS.probe, daemon=True).start()
    GAMES[0] = GameTracker(sampler)
    History(sampler, kmt).start()
    threading.Thread(target=auto_update_loop, daemon=True).start()
    widget = DesktopWidget(sampler, kmt)
    WIDGET_HOOK[0] = widget.apply_settings
    HOTKEY[0] = Hotkey(lambda: widget.set_visible(not WIDGET_ON[0]))
    hk = load_settings().get("hotkey")
    if isinstance(hk, dict) and hk.get("vk"):
        threading.Thread(target=lambda: HOTKEY[0].set(hk.get("mods", 0), hk.get("vk")), daemon=True).start()
    if load_settings().get("widget"):
        widget.apply_settings()
    sampler.selfuse.window_pid = wm.pid

    if "--no-window" in sys.argv:
        print("%s : %s  (Ctrl+C pour quitter)" % (APP_NAME, url))
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            quit_app()

    if wm.native and _has_module("pystray") and _has_module("PIL"):
        import pystray
        stable_tray_id()
        txt = TRAY_TEXT.get(load_settings().get("lang", "fr"), TRAY_TEXT["fr"])
        icon = pystray.Icon("pulse_monitor", tray_image(), "%s v%s" % (APP_NAME, APP_VERSION),
                            menu=pystray.Menu(
                                pystray.MenuItem(txt["open"], lambda *_: wm.open(), default=True),
                                pystray.MenuItem(txt["widget"], lambda *_: widget.set_visible(not WIDGET_ON[0]),
                                                 checked=lambda _i: WIDGET_ON[0]),
                                pystray.MenuItem(txt["pin"], lambda *_: widget.set_pinned(not widget.pinned()),
                                                 checked=lambda _i: widget.pinned()),
                                pystray.Menu.SEPARATOR,
                                pystray.MenuItem(txt["quit"], quit_app)))

        def refresh_menu():
            try:
                icon.update_menu()
            except Exception:
                pass
        widget.on_change = refresh_menu

        def tooltip_loop():
            # calcul indépendant (ne perturbe pas les mesures de la fenêtre)
            prev = psutil.cpu_times()
            while True:
                time.sleep(10)
                try:
                    cur = psutil.cpu_times()
                    tot = sum(cur) - sum(prev)
                    cpu = 100.0 * (1 - (cur.idle - prev.idle) / tot) if tot > 0 else 0.0
                    prev = cur
                    icon.title = "%s · CPU %d %% · RAM %d %%" % (APP_NAME, max(0, cpu), psutil.virtual_memory().percent)
                except Exception:
                    pass

        def setup(ic):
            ic.visible = True
            if "--startup" not in sys.argv:   # lancé avec Windows : reste discret près de l'horloge
                wm.open()
            threading.Thread(target=tooltip_loop, daemon=True).start()
            threading.Timer(3, refresh_menu).start()   # état du widget au démarrage
        log.info("Tray icon active")
        icon.run(setup=setup)   # bloque jusqu'à « Quitter »
        quit_app()

    # Sans icône de notification : comportement classique (fermer = quitter)
    wm.open()
    if wm.native:
        while wm.is_open() or wm.proc is None:
            time.sleep(1)
        quit_app()
    # Navigateur : on s'arrête quand la page n'est plus ouverte.
    LAST_PING[0] = time.time() + 30
    while time.time() - LAST_PING[0] < 150:
        time.sleep(5)
    quit_app()


if __name__ == "__main__":
    main()
