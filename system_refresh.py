#!/usr/bin/env python3
"""
system_refresh: prune resource bloat on a running machine, the next best thing to a reboot.

Runs on Windows 10/11, macOS and Linux (RHEL 8+). Needs Python 3.6+ and psutil.

Each step has a mode in refresh_config.json (written with safe defaults on first run):
"on" runs every time, "deep" only with --deep, "off" never.

  kill_hung_apps        end apps whose windows stay "Not Responding"          Windows
  stop_background_apps  stop helper and updater processes you list by name    all
  prune_orphans         end dev-tool process trees whose parent has died      all
  restart_shell         restart explorer.exe or Finder once it has bloated    Windows, macOS
  trim_working_sets     push idle pages of background apps out of RAM         Windows
  purge_memory_caches   drop the standby list / file cache                    all, admin or root
  clean_temp_files      delete stale temp files, crash dumps, error reports   all
  flush_dns             clear the DNS resolver cache                          all

Examples:
  python3 system_refresh.py --dry-run           show what a standard run would do
  python3 system_refresh.py --deep --elevate    run everything as administrator / root
  python3 system_refresh.py --only trim_working_sets,flush_dns
  python3 system_refresh.py --install --every 4 --require-idle 10
"""

import argparse
import fnmatch
import getpass
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

try:
    import psutil
except ImportError:
    sys.stderr.write(
        "system_refresh needs the psutil package. Install it with:\n"
        "  Windows:  py -m pip install psutil\n"
        "  macOS:    python3 -m pip install --user psutil\n"
        "  RHEL:     sudo dnf install python3-psutil   (or: python3 -m pip install --user psutil)\n")
    sys.exit(2)

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MAC = SYSTEM == "Darwin"
IS_LINUX = not (IS_WINDOWS or IS_MAC)
OS_KEY = "windows" if IS_WINDOWS else "darwin" if IS_MAC else "linux"
OS_NAME = {"windows": "Windows", "darwin": "macOS", "linux": "Linux"}[OS_KEY]
OS_KEYS = {"windows", "darwin", "linux"}

SCRIPT_PATH = os.path.abspath(__file__)
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "refresh_config.json")
PSUTIL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(psutil.__file__)))
TASK_NAME = "SystemRefresh"
LAUNCHD_LABEL = "com.systemrefresh"
CRON_MARKER = "# system_refresh"
MB = 1024 * 1024
GB = 1024 * MB

STEP_ORDER = [
    "kill_hung_apps", "stop_background_apps", "prune_orphans", "restart_shell",
    "trim_working_sets", "purge_memory_caches", "clean_temp_files", "flush_dns",
]

# Any setting can be a plain value or a {"windows": ..., "darwin": ..., "linux": ...} dict.
DEFAULT_CONFIG = {
    "dry_run": False,
    "log_directory": "logs",
    "keep_logs": 30,
    "protected_processes": [],
    "steps": {
        "kill_hung_apps": {"mode": "deep", "confirm_seconds": 10},
        "stop_background_apps": {
            "mode": "on",
            "names": {
                "windows": ["Widgets", "WidgetService", "GameBar", "GameBarFTServer", "jusched", "AdobeARM"],
                "darwin": [],
                "linux": [],
            },
        },
        "prune_orphans": {
            "mode": "deep",
            "min_age_minutes": 120,
            "names": ["node", "python*", "java", "javaw", "dotnet", "msbuild", "vbcscompiler",
                      "conhost", "esbuild", "rg", "ruby", "deno", "bun"],
        },
        "restart_shell": {"mode": "deep", "min_memory_mb": 600},
        "trim_working_sets": {"mode": "on", "min_working_set_mb": 100, "skip_foreground_app": True},
        "purge_memory_caches": {"mode": "deep", "flush_modified_list": True, "compact_memory": True},
        "clean_temp_files": {
            "mode": "on",
            "older_than_days": 3,
            "paths": {
                "windows": [
                    "%TEMP%",
                    "%WINDIR%\\Temp",
                    "%LOCALAPPDATA%\\CrashDumps",
                    "%LOCALAPPDATA%\\Microsoft\\Windows\\WER\\ReportArchive",
                    "%LOCALAPPDATA%\\Microsoft\\Windows\\WER\\ReportQueue",
                    "%PROGRAMDATA%\\Microsoft\\Windows\\WER\\ReportArchive",
                    "%PROGRAMDATA%\\Microsoft\\Windows\\WER\\ReportQueue",
                ],
                "darwin": ["$TMPDIR", "~/Library/Logs/DiagnosticReports"],
                "linux": ["/tmp", "/var/tmp"],
            },
        },
        "flush_dns": {"mode": "on"},
    },
    "report": {"top_count": 8, "heavy_app_mb": 1500, "handle_leak_threshold": 10000},
}

# Never stopped or trimmed, whatever the config says (fnmatch patterns, lowercase, no ".exe").
PROTECTED = {
    "windows": [
        "idle", "system", "system idle process", "secure system", "registry", "memory compression",
        "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso", "svchost", "dwm",
        "fontdrvhost", "sihost", "ctfmon", "audiodg", "spoolsv", "msmpeng", "nissrv",
        "securityhealthservice", "sgrmbroker", "wudfhost", "explorer", "taskhostw",
        "startmenuexperiencehost", "shellexperiencehost", "searchhost", "textinputhost",
        "runtimebroker", "dllhost", "wmiprvse", "vmmem*", "vmcompute", "vmwp", "msiexec",
        "trustedinstaller", "tiworker",
    ],
    "darwin": [
        "kernel_task", "launchd", "windowserver", "loginwindow", "dock", "finder", "systemuiserver",
        "controlcenter", "coreaudiod", "mds", "mds_stores", "mdworker*", "opendirectoryd", "securityd",
        "trustd", "cfprefsd", "distnoted", "usernoted", "notifyd", "logd", "syslogd", "configd",
        "powerd", "bluetoothd", "airportd", "softwareupdated", "installd", "backupd",
    ],
    "linux": [
        "systemd", "systemd-*", "init", "kthreadd", "dbus-daemon", "dbus-broker", "xorg", "xwayland",
        "gnome-shell", "kwin_*", "plasmashell", "gdm*", "sddm*", "sshd", "login", "pipewire*",
        "wireplumber", "pulseaudio", "networkmanager", "polkitd", "rsyslogd", "auditd", "chronyd",
        "firewalld", "tuned", "dnf", "yum", "rpm", "packagekitd", "sssd*",
    ],
}


class StepSkipped(Exception):
    """A step that cannot or should not run here; the message says why."""


# ---------------------------------------------------------------- Windows API

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _pdh = ctypes.WinDLL("pdh")

    def _api(dll, name, restype, *argtypes):
        function = getattr(dll, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    class _LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class _TOKEN_PRIVILEGES(ctypes.Structure):
        # One LUID_AND_ATTRIBUTES entry, flattened.
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Luid", _LUID), ("Attributes", wintypes.DWORD)]

    class _PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD)] + [
            (field, ctypes.c_size_t) for field in (
                "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
                "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")
        ] + [("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD), ("ThreadCount", wintypes.DWORD)]

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("largeValue", ctypes.c_longlong)]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _OpenProcess = _api(_kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _CloseHandle = _api(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _GetCurrentProcess = _api(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
    _ProcessIdToSessionId = _api(_kernel32, "ProcessIdToSessionId", wintypes.BOOL,
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _SetSystemFileCacheSize = _api(_kernel32, "SetSystemFileCacheSize", wintypes.BOOL,
                                   ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD)
    _GetTickCount = _api(_kernel32, "GetTickCount", wintypes.DWORD)
    _GetStdHandle = _api(_kernel32, "GetStdHandle", wintypes.HANDLE, wintypes.DWORD)
    _GetConsoleMode = _api(_kernel32, "GetConsoleMode", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _SetConsoleMode = _api(_kernel32, "SetConsoleMode", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD)
    _EmptyWorkingSet = _api(_psapi, "EmptyWorkingSet", wintypes.BOOL, wintypes.HANDLE)
    _GetPerformanceInfo = _api(_psapi, "GetPerformanceInfo", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD)
    _EnumWindows = _api(_user32, "EnumWindows", wintypes.BOOL, _WNDENUMPROC, wintypes.LPARAM)
    _IsWindowVisible = _api(_user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
    _IsHungAppWindow = _api(_user32, "IsHungAppWindow", wintypes.BOOL, wintypes.HWND)
    _GetWindow = _api(_user32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
    _GetWindowThreadProcessId = _api(_user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                     wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _GetWindowTextW = _api(_user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _GetForegroundWindow = _api(_user32, "GetForegroundWindow", wintypes.HWND)
    _GetLastInputInfo = _api(_user32, "GetLastInputInfo", wintypes.BOOL, ctypes.c_void_p)
    _IsUserAnAdmin = _api(_shell32, "IsUserAnAdmin", wintypes.BOOL)
    _ShellExecuteW = _api(_shell32, "ShellExecuteW", ctypes.c_void_p, wintypes.HWND, wintypes.LPCWSTR,
                          wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int)
    _OpenProcessToken = _api(_advapi32, "OpenProcessToken", wintypes.BOOL,
                             wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _LookupPrivilegeValueW = _api(_advapi32, "LookupPrivilegeValueW", wintypes.BOOL,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_LUID))
    _AdjustTokenPrivileges = _api(_advapi32, "AdjustTokenPrivileges", wintypes.BOOL, wintypes.HANDLE,
                                  wintypes.BOOL, ctypes.POINTER(_TOKEN_PRIVILEGES), wintypes.DWORD,
                                  ctypes.c_void_p, ctypes.c_void_p)
    _NtSetSystemInformation = _api(_ntdll, "NtSetSystemInformation", ctypes.c_long,
                                   ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong)
    _PdhOpenQueryW = _api(_pdh, "PdhOpenQueryW", ctypes.c_long,
                          wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE))
    _PdhAddEnglishCounterW = _api(_pdh, "PdhAddEnglishCounterW", ctypes.c_long, wintypes.HANDLE,
                                  wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE))
    _PdhCollectQueryData = _api(_pdh, "PdhCollectQueryData", ctypes.c_long, wintypes.HANDLE)
    _PdhGetFormattedCounterValue = _api(_pdh, "PdhGetFormattedCounterValue", ctypes.c_long, wintypes.HANDLE,
                                        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(_PDH_FMT_COUNTERVALUE))
    _PdhCloseQuery = _api(_pdh, "PdhCloseQuery", ctypes.c_long, wintypes.HANDLE)

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    STATUS_PRIVILEGE_NOT_HELD = 0xC0000061
    MEMORY_FLUSH_MODIFIED_LIST = 3
    MEMORY_PURGE_STANDBY_LIST = 4

    _MEMORY_COUNTERS = [
        ("free", r"\Memory\Free & Zero Page List Bytes"),
        ("standby", r"\Memory\Standby Cache Core Bytes"),
        ("standby", r"\Memory\Standby Cache Normal Priority Bytes"),
        ("standby", r"\Memory\Standby Cache Reserve Bytes"),
        ("modified", r"\Memory\Modified Page List Bytes"),
    ]

    def win_session_id(pid):
        session = wintypes.DWORD()
        return session.value if _ProcessIdToSessionId(pid, ctypes.byref(session)) else None

    def win_top_windows():
        """(hwnd, pid) for every visible, unowned top-level window."""
        windows = []

        def visit(hwnd, _):
            if _IsWindowVisible(hwnd) and not _GetWindow(hwnd, GW_OWNER):
                pid = wintypes.DWORD()
                _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                windows.append((hwnd, pid.value))
            return True

        _EnumWindows(_WNDENUMPROC(visit), 0)
        return windows

    def win_hung_windows():
        """pid -> window title for apps whose top-level window has stopped responding."""
        hung = {}
        for hwnd, pid in win_top_windows():
            if pid not in hung and _IsHungAppWindow(hwnd):
                # GetWindowText reads another process's caption without messaging it, so it can't hang here.
                title = ctypes.create_unicode_buffer(256)
                _GetWindowTextW(hwnd, title, 256)
                hung[pid] = title.value
        return hung

    def win_foreground_pid():
        pid = wintypes.DWORD()
        hwnd = _GetForegroundWindow()
        if hwnd:
            _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def win_trim(pid, check_only=False):
        """Empties a process's working set; the pages stay in RAM on the standby or modified list."""
        handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
        if not handle:
            return False
        try:
            return check_only or bool(_EmptyWorkingSet(handle))
        finally:
            _CloseHandle(handle)

    def win_enable_privilege(name):
        """Returns 0 on success, otherwise a Win32 error code (1300: the account lacks the privilege)."""
        token = wintypes.HANDLE()
        if not _OpenProcessToken(_GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
            return ctypes.get_last_error()
        try:
            privileges = _TOKEN_PRIVILEGES(PrivilegeCount=1, Attributes=2)  # SE_PRIVILEGE_ENABLED
            if not _LookupPrivilegeValueW(None, name, ctypes.byref(privileges.Luid)):
                return ctypes.get_last_error()
            ctypes.set_last_error(0)
            _AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None)
            return ctypes.get_last_error()
        finally:
            _CloseHandle(token)

    def win_flush_file_cache():
        """Shrinks the system file cache to nothing; returns 0 or a Win32 error code."""
        error = win_enable_privilege("SeIncreaseQuotaPrivilege")
        if error:
            return error
        unlimited = ctypes.c_size_t(-1).value
        return 0 if _SetSystemFileCacheSize(unlimited, unlimited, 0) else ctypes.get_last_error()

    def win_memory_list_command(command):
        """Runs a SystemMemoryListInformation command; returns an NTSTATUS (0 = success)."""
        if win_enable_privilege("SeProfileSingleProcessPrivilege"):
            return STATUS_PRIVILEGE_NOT_HELD
        value = ctypes.c_int(command)
        return _NtSetSystemInformation(80, ctypes.byref(value), ctypes.sizeof(value)) & 0xFFFFFFFF

    def win_performance_info():
        info = _PERFORMANCE_INFORMATION(cb=ctypes.sizeof(_PERFORMANCE_INFORMATION))
        return info if _GetPerformanceInfo(ctypes.byref(info), info.cb) else None

    def win_memory_lists():
        """Bytes on the free, standby and modified page lists, from English-named performance counters."""
        query = wintypes.HANDLE()
        if _PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
            return {}
        try:
            counters = []
            for key, path in _MEMORY_COUNTERS:
                counter = wintypes.HANDLE()
                if _PdhAddEnglishCounterW(query, path, None, ctypes.byref(counter)) == 0:
                    counters.append((key, counter))
            if _PdhCollectQueryData(query) != 0:
                return {}
            totals = {}
            for key, counter in counters:
                value = _PDH_FMT_COUNTERVALUE()
                if _PdhGetFormattedCounterValue(counter, 0x0400 | 0x1000, None, ctypes.byref(value)) == 0:
                    totals[key] = totals.get(key, 0) + value.largeValue  # PDH_FMT_LARGE | PDH_FMT_NOSCALE
            return totals
        finally:
            _PdhCloseQuery(query)

    def win_idle_seconds():
        info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
        if not _GetLastInputInfo(ctypes.byref(info)):
            return None
        return ((_GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0

    def win_enable_ansi():
        handle = _GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not _GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(_SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


# ---------------------------------------------------------------- small helpers

def fmt_bytes(value, signed=False):
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    value = abs(value)
    if value >= GB:
        return "%s%.2f GB" % (sign, value / GB)
    if value >= MB:
        return "%s%.0f MB" % (sign, value / MB)
    if value >= 1024:
        return "%s%.0f KB" % (sign, value / 1024)
    return "%s%d B" % (sign, value)


def fmt_age(days):
    for unit, size in (("day", 1.0), ("hour", 1 / 24.0), ("minute", 1 / 1440.0)):
        if days >= size:
            count = days / size
            return "%g %s%s" % (round(count, 1), unit, "" if count == 1 else "s")
    return "%d seconds" % round(days * 86400)


def fmt_metric(kind, value, signed=False):
    if kind == "bytes":
        return fmt_bytes(value, signed)
    return ("{:+,}" if signed and value else "{:,}").format(int(value))


def strip_exe(name):
    return name[:-4] if name.lower().endswith(".exe") else name


def norm(name):
    """Process names compare lowercase and without ".exe", so one list works on every OS."""
    return strip_exe(name or "").lower()


def matches(name, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def for_this_os(value, default=None):
    if isinstance(value, dict) and value and set(value) <= OS_KEYS:
        value = value.get(OS_KEY)
    return default if value is None else value


def run(command, timeout=60):
    """Runs a command and returns (exit code, combined output); 127 if it doesn't exist."""
    flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW: no console flash under pythonw
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, errors="replace", timeout=timeout,
                                creationflags=flags)
    except FileNotFoundError:
        return 127, "%s: command not found" % command[0]
    except subprocess.TimeoutExpired:
        return 124, "%s: timed out" % command[0]
    return result.returncode, result.stdout.strip()


def is_admin():
    if IS_WINDOWS:
        try:
            return bool(_IsUserAnAdmin())
        except OSError:
            return False
    return os.geteuid() == 0


def app_memory(proc):
    """Private bytes on Windows, resident size elsewhere; 0 when the process can't be read."""
    try:
        info = proc.memory_info()
    except psutil.Error:
        return 0
    if IS_WINDOWS:
        return getattr(info, "private", 0) or info.vms
    return info.rss


def idle_seconds():
    if IS_WINDOWS:
        return win_idle_seconds()
    if IS_MAC:
        code, out = run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])
        for line in out.splitlines():
            if '"HIDIdleTime"' in line:
                try:
                    return int(line.rsplit("=", 1)[1].strip()) / 1e9
                except ValueError:
                    return None
    return None


def ignores_sighup(pid):
    """True for processes started with nohup: their owner meant them to outlive the terminal."""
    if IS_LINUX:
        try:
            with open("/proc/%d/status" % pid) as status:
                for line in status:
                    if line.startswith("SigIgn:"):
                        return bool(int(line.split()[1], 16) & 1)
        except (OSError, ValueError):
            pass
    elif IS_MAC:
        code, out = run(["ps", "-o", "sigignore=", "-p", str(pid)])
        try:
            return code == 0 and bool(int(out.strip(), 16) & 1)
        except ValueError:
            pass
    return False


def systemd_unit(pid):
    """The leaf of a Linux process's systemd cgroup, e.g. "nginx.service" or "session-3.scope"."""
    try:
        with open("/proc/%d/cgroup" % pid) as cgroups:
            for line in cgroups:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[1] in ("", "name=systemd"):
                    return parts[2].rstrip("/").rsplit("/", 1)[-1]
    except OSError:
        pass
    return ""


def launchd_job_pids(uid):
    """PIDs launchd started on purpose (agents, daemons, apps); they look orphaned but aren't."""
    command = ["launchctl", "list"]
    if os.geteuid() == 0 and uid != 0:
        command = ["launchctl", "asuser", str(uid)] + command
    code, out = run(command)
    pids = set()
    for line in out.splitlines()[1:]:
        first = line.split(None, 1)[0] if line.strip() else ""
        if first.isdigit():
            pids.add(int(first))
    return pids


def own_ancestry():
    """This process and every process above it, which a cleanup must never end."""
    pids = {0, 4} if IS_WINDOWS else {0, 1}
    pid = os.getpid()
    while pid not in pids:
        pids.add(pid)
        try:
            pid = psutil.Process(pid).ppid()
        except psutil.Error:
            break
    return pids


# ---------------------------------------------------------------- logging and settings

class Log(object):
    TAGS = {"info": "    ", "detail": "    ", "ok": " ok ", "act": " >> ", "warn": "WARN", "skip": "skip", "head": "===="}
    COLORS = {"ok": "32", "act": "36", "warn": "33", "skip": "90", "head": "1"}

    def __init__(self, quiet):
        self.quiet = quiet
        self.path = None
        self.file = None
        stdout = sys.stdout
        self.color = bool(not quiet and stdout and stdout.isatty() and "NO_COLOR" not in os.environ)
        if self.color and IS_WINDOWS:
            self.color = win_enable_ansi()

    def open(self, path):
        self.path = path
        self.file = open(path, "a", encoding="utf-8")

    def __call__(self, message, level="info"):
        if self.file:
            self.file.write("%s [%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), self.TAGS[level], message))
            self.file.flush()
        if self.quiet or level == "detail":
            return
        if level == "head":
            print()
            text = "== " + message
        else:
            text = "  %s %s" % (self.TAGS[level], message)
        if self.color and level in self.COLORS:
            text = "\033[%sm%s\033[0m" % (self.COLORS[level], text)
        print(text)


def deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path):
    """Returns (settings, created): the file merged over the defaults, writing the defaults if it's missing."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as source:
            deep_merge(config, json.load(source))
        return config, False
    with open(path, "w", encoding="utf-8") as target:
        json.dump(DEFAULT_CONFIG, target, indent=2)
        target.write("\n")
    return config, True


class Context(object):
    """Settings and identity shared by every step."""

    def __init__(self, args, config, log):
        self.args = args
        self.config = config
        self.log = log
        self.simulate = bool(args.dry_run or config.get("dry_run"))
        self.is_admin = is_admin()
        self.untouchable = own_ancestry()
        extra = [norm(name) for name in for_this_os(config.get("protected_processes"), [])]
        self.protected = PROTECTED[OS_KEY] + extra
        if IS_WINDOWS:
            self.session = win_session_id(os.getpid())
            self.user = getpass.getuser()
            self.uid = None
            self.home = os.path.expanduser("~")
        else:
            import pwd
            self.user = args.target_user or os.environ.get("SUDO_USER") or getpass.getuser()
            entry = pwd.getpwnam(self.user)
            self.uid, self.gid, self.home = entry.pw_uid, entry.pw_gid, entry.pw_dir
            if IS_MAC and (os.geteuid() == 0 or not os.environ.get("TMPDIR")):
                command = ["getconf", "DARWIN_USER_TEMP_DIR"]
                if os.geteuid() == 0 and self.user != "root":
                    command = ["sudo", "-u", self.user] + command
                code, out = run(command)
                if code == 0 and out:
                    os.environ["TMPDIR"] = out

    def is_protected(self, name):
        return matches(norm(name), self.protected)

    def owns(self, proc):
        """True for processes in this desktop session (Windows) or owned by the target user (Unix)."""
        try:
            if IS_WINDOWS:
                return win_session_id(proc.pid) == self.session
            return proc.uids().real == self.uid
        except psutil.Error:
            return False

    def expand(self, raw):
        path = raw
        if path.startswith("~"):
            path = self.home + path[1:]
        path = os.path.expandvars(path)
        if "$" in path or "%" in path:  # an unset variable
            return None
        return os.path.normpath(path)

    def give_back(self, path):
        """Under sudo, hand files this run creates back to the user."""
        if not IS_WINDOWS and os.geteuid() == 0 and self.uid:
            try:
                os.chown(path, self.uid, self.gid)
            except OSError:
                pass


def stop_processes(ctx, procs, reason):
    """Ends processes: a polite terminate, then a kill after 3 s. Returns (count, bytes they held)."""
    victims = []
    for proc in procs:
        try:
            victims.append((proc, strip_exe(proc.name()), app_memory(proc)))
        except psutil.Error:
            pass
    for proc, name, memory in victims:
        verb = "would stop" if ctx.simulate else "stopping"
        ctx.log("%s %s [%d], %s: %s" % (verb, name, proc.pid, fmt_bytes(memory), reason), "act")
    if ctx.simulate:
        return len(victims), sum(memory for _, _, memory in victims)
    signalled = []
    for proc, name, _ in victims:
        try:
            proc.terminate()  # psutil refuses if the PID now belongs to a different process
            signalled.append(proc)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            ctx.log("access denied stopping %s [%d]" % (name, proc.pid), "warn")
    _, alive = psutil.wait_procs(signalled, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=3)
    stopped = {proc.pid for proc in signalled}
    return len(stopped), sum(memory for proc, _, memory in victims if proc.pid in stopped)


# ---------------------------------------------------------------- steps

def step_kill_hung_apps(ctx, cfg):
    if not IS_WINDOWS:
        raise StepSkipped("not available on %s" % OS_NAME)
    hung = []
    for pid, title in win_hung_windows().items():
        try:
            proc = psutil.Process(pid)
            name = strip_exe(proc.name())
        except psutil.Error:
            continue
        if pid in ctx.untouchable or ctx.is_protected(name) or not ctx.owns(proc):
            continue
        ctx.log("not responding: %s [%d] '%s'" % (name, pid, title), "warn")
        hung.append((proc, name))
    if not hung:
        return "no app is hung"
    wait = int(cfg.get("confirm_seconds", 10))
    if ctx.simulate:
        return "would end %d app(s) if still frozen after %d s" % (len(hung), wait)
    time.sleep(wait)
    still_hung = win_hung_windows()
    frozen = []
    for proc, name in hung:
        if proc.pid in still_hung and proc.is_running():
            frozen.append(proc)
        else:
            ctx.log("%s [%d] recovered; left running" % (name, proc.pid), "ok")
    if not frozen:
        return "every hung app recovered"
    count, freed = stop_processes(ctx, frozen, "stayed not responding")
    return "ended %d hung app(s), %s" % (count, fmt_bytes(freed))


def step_stop_background_apps(ctx, cfg):
    patterns = [norm(name) for name in for_this_os(cfg.get("names"), [])]
    if not patterns:
        raise StepSkipped("no app names are listed for %s" % OS_NAME)
    targets = []
    for proc in psutil.process_iter(["name"]):
        name = strip_exe(proc.info["name"] or "")
        if not matches(norm(name), patterns):
            continue
        if ctx.is_protected(name):
            ctx.log("%s is a protected process; remove it from stop_background_apps" % name, "warn")
        elif proc.pid not in ctx.untouchable and ctx.owns(proc):
            targets.append(proc)
    if not targets:
        return "none of the %d listed apps were running" % len(patterns)
    count, freed = stop_processes(ctx, targets, "listed background app")
    return "%s %d process(es), %s" % ("would stop" if ctx.simulate else "stopped", count, fmt_bytes(freed))


def is_orphan(proc, procs):
    parent = procs.get(proc.info["ppid"])
    if IS_WINDOWS:
        # Windows keeps a dead parent's PID: the parent is gone, or the PID now names a younger process.
        return parent is None or (parent.info["create_time"] or 0) > proc.info["create_time"]
    if proc.info["ppid"] == 1:
        return True
    # Linux hands orphans to the nearest subreaper, normally `systemd --user`.
    return IS_LINUX and parent is not None and norm(parent.info["name"]) == "systemd"


def collect_tree(root, children, handled):
    """The root and its descendants, breadth first."""
    tree, queue = [], [root]
    while queue:
        node = queue.pop(0)
        if node.pid in handled:
            continue
        handled.add(node.pid)
        tree.append(node)
        born = node.info["create_time"] or 0
        for kid in children.get(node.pid, []):
            # A "child" older than its parent is a recycled PID, not a real child.
            if (kid.info["create_time"] or 0) >= born:
                queue.append(kid)
    return tree


def keep_reason(ctx, tree, windowed, launchd_jobs):
    """Why an orphaned tree must be left alone, or None if it can go."""
    for node in tree:
        name = strip_exe(node.info["name"] or "")
        if node.pid in ctx.untouchable:
            return "it contains this script"
        if ctx.is_protected(name):
            return "%s is protected" % name
        if node.pid in windowed:
            return "%s has a window open" % name
        if node.pid in launchd_jobs:
            return "%s is a launchd job" % name
        if IS_LINUX and systemd_unit(node.pid).endswith(".service"):
            return "%s runs as %s" % (name, systemd_unit(node.pid))
    if ignores_sighup(tree[0].pid):
        return "it was started with nohup"
    return None


def step_prune_orphans(ctx, cfg):
    patterns = [norm(name) for name in for_this_os(cfg.get("names"), [])]
    cutoff = time.time() - float(cfg.get("min_age_minutes", 120)) * 60
    procs, children = {}, {}
    for proc in psutil.process_iter(["ppid", "name", "create_time"]):
        procs[proc.pid] = proc
        children.setdefault(proc.info["ppid"], []).append(proc)
    windowed = {pid for _, pid in win_top_windows()} if IS_WINDOWS else set()
    launchd_jobs = launchd_job_pids(ctx.uid) if IS_MAC else set()
    handled = set()
    trees = count = freed = 0
    for proc in list(procs.values()):
        name = strip_exe(proc.info["name"] or "")
        started = proc.info["create_time"]
        if proc.pid in handled or not started or started > cutoff:
            continue
        if not matches(norm(name), patterns) or not is_orphan(proc, procs) or not ctx.owns(proc):
            continue
        tree = collect_tree(proc, children, handled)
        label = "%s [%d]: parent gone, running %.1f h, %d process(es)" % (
            name, proc.pid, (time.time() - started) / 3600, len(tree))
        reason = keep_reason(ctx, tree, windowed, launchd_jobs)
        if reason:
            ctx.log("%s; kept because %s" % (label, reason), "skip")
            continue
        ctx.log(label)
        trees += 1
        stopped, memory = stop_processes(ctx, reversed(tree), "orphaned %s tree" % name)
        count += stopped
        freed += memory
    if not trees:
        return "no orphaned process trees"
    verb = "would end" if ctx.simulate else "ended"
    return "%s %d orphaned tree(s): %d process(es), %s" % (verb, trees, count, fmt_bytes(freed))


def step_restart_shell(ctx, cfg):
    if IS_LINUX:
        raise StepSkipped("not available on Linux")
    if IS_WINDOWS and ctx.is_admin:
        raise StepSkipped("from an elevated run Explorer would come back elevated; run without --elevate")
    shell, label = ("explorer", "Explorer") if IS_WINDOWS else ("finder", "Finder")

    def shell_processes():
        return [p for p in psutil.process_iter(["name"]) if norm(p.info["name"]) == shell and ctx.owns(p)]

    procs = shell_processes()
    if not procs:
        raise StepSkipped("%s is not running" % label)
    before = sum(app_memory(p) for p in procs)
    if before < float(cfg.get("min_memory_mb", 600)) * MB:
        return "%s is lean (%s); left alone" % (label, fmt_bytes(before))
    if ctx.simulate:
        return "would restart %s (%s)" % (label, fmt_bytes(before))
    for proc in procs:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)
    # Windows and macOS usually relaunch the shell by themselves; start it if they haven't.
    deadline = time.time() + 8
    while time.time() < deadline and not shell_processes():
        time.sleep(0.5)
    if not shell_processes():
        if IS_WINDOWS:
            subprocess.Popen(["explorer.exe"])
        elif not ctx.is_admin:
            subprocess.Popen(["open", "-a", "Finder"])
        time.sleep(3)
    after = sum(app_memory(p) for p in shell_processes())
    return "restarted %s: %s -> %s" % (label, fmt_bytes(before), fmt_bytes(after))


def step_trim_working_sets(ctx, cfg):
    if not IS_WINDOWS:
        raise StepSkipped("%s offers no way to trim other apps' memory" % OS_NAME)
    minimum = float(cfg.get("min_working_set_mb", 100)) * MB
    foreground = None
    if cfg.get("skip_foreground_app", True):
        pid = win_foreground_pid()
        try:
            foreground = norm(psutil.Process(pid).name()) if pid else None
        except psutil.Error:
            pass
        if foreground:
            ctx.log("leaving the app in front alone: %s" % foreground, "skip")
    candidates = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        info = proc.info["memory_info"]
        name = strip_exe(proc.info["name"] or "")
        if info is None or info.rss < minimum:
            continue
        if proc.pid in ctx.untouchable or ctx.is_protected(name) or norm(name) == foreground:
            continue
        candidates.append((info.rss, proc, name))
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    trimmed = denied = released = 0
    for before, proc, name in candidates:
        if not win_trim(proc.pid, check_only=ctx.simulate):
            denied += 1
            continue
        after = before
        if not ctx.simulate:
            try:
                after = proc.memory_info().rss
            except psutil.Error:
                pass
        trimmed += 1
        released += before if ctx.simulate else max(0, before - after)
        ctx.log("%s %s [%d]: %s -> %s" % ("would trim" if ctx.simulate else "trimmed", name, proc.pid,
                                          fmt_bytes(before), "?" if ctx.simulate else fmt_bytes(after)), "detail")
    note = ""
    if denied:
        note = "; %d need administrator" % denied if not ctx.is_admin else "; %d refused" % denied
    if ctx.simulate:
        return "would trim %d process(es) holding %s%s" % (trimmed, fmt_bytes(released), note)
    return "trimmed %d process(es), %s moved out of working sets%s" % (trimmed, fmt_bytes(released), note)


def step_purge_memory_caches(ctx, cfg):
    if not ctx.is_admin:
        raise StepSkipped("needs %s (add --elevate)" % ("administrator" if IS_WINDOWS else "root"))
    flush_modified = cfg.get("flush_modified_list", True)
    compact = cfg.get("compact_memory", True)
    if IS_WINDOWS:
        plan = ["flush the file cache"] + (["write out the modified list"] if flush_modified else []) + \
               ["purge the standby list"]
    elif IS_MAC:
        plan = ["sync disks", "purge the disk cache"]
    else:
        plan = ["sync disks", "drop the page, dentry and inode caches"] + (["compact memory"] if compact else [])
    if ctx.simulate:
        return "would " + ", ".join(plan)
    done = []
    if IS_WINDOWS:
        error = win_flush_file_cache()
        if error:
            ctx.log("file cache flush failed (Win32 error %d)" % error, "warn")
        else:
            done.append("file cache flushed")
        commands = [(MEMORY_PURGE_STANDBY_LIST, "standby list purge", "standby list purged")]
        if flush_modified:
            commands.insert(0, (MEMORY_FLUSH_MODIFIED_LIST, "modified list flush", "modified list written out"))
        for command, action, outcome in commands:
            status = win_memory_list_command(command)
            if status:
                ctx.log("%s failed (NTSTATUS 0x%08X)" % (action, status), "warn")
            else:
                done.append(outcome)
    elif IS_MAC:
        os.sync()
        code, out = run(["purge"], timeout=300)
        if code:
            raise RuntimeError("purge failed: %s" % out)
        done.append("disk cache purged")
    else:
        os.sync()
        with open("/proc/sys/vm/drop_caches", "w") as knob:
            knob.write("3\n")
        done.append("page, dentry and inode caches dropped")
        if compact and os.path.exists("/proc/sys/vm/compact_memory"):
            with open("/proc/sys/vm/compact_memory", "w") as knob:
                knob.write("1\n")
            done.append("memory compacted")
    return ", ".join(done) or "nothing was purged"


def unsafe_root(path, ctx):
    """True for folders a temp cleanup must never start from: drive roots, system and home folders."""
    path = os.path.normcase(os.path.realpath(path)).rstrip("\\/")
    if len(path) <= 2:  # "", "C:"
        return True
    critical = [ctx.home]
    if IS_WINDOWS:
        critical += [os.environ.get(name) for name in (
            "WINDIR", "USERPROFILE", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
            "LOCALAPPDATA", "APPDATA", "PUBLIC")]
    else:
        critical += ["/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/opt", "/proc", "/root", "/sbin",
                     "/srv", "/sys", "/usr", "/var", "/Applications", "/Library", "/System", "/Users",
                     "/Volumes", "/private", "/private/var"]
    for folder in critical:
        if not folder:
            continue
        folder = os.path.normcase(os.path.realpath(folder)).rstrip("\\/")
        if path == folder or folder.startswith(path + os.sep):
            return True
    return False


def newest_time(st):
    # Creation counts too: installers unpack files that keep their original, older modified dates.
    if IS_WINDOWS:  # st_ctime is creation time on Windows today, but newer Pythons are retiring that meaning
        return max(st.st_mtime, getattr(st, "st_birthtime", st.st_ctime))
    return max(st.st_mtime, st.st_ctime, getattr(st, "st_birthtime", 0))


def clean_tree(ctx, root, cutoff):
    """Deletes old regular files under root. Returns (files, bytes, files left in use, folders removed)."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    files = size = busy = 0
    root_device = os.stat(root).st_dev
    folders = []  # (path, modified time before cleanup)
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # Never follow links or junctions out of the temp folder.
            if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & reparse:
                continue
            if not IS_WINDOWS and (st.st_dev != root_device or st.st_uid != ctx.uid):
                continue
            if stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
                folders.append((entry.path, st.st_mtime))
                continue
            if not stat.S_ISREG(st.st_mode) or newest_time(st) >= cutoff:
                continue
            if not ctx.simulate:
                try:
                    os.unlink(entry.path)
                except PermissionError:
                    if not (IS_WINDOWS and not st.st_mode & stat.S_IWRITE):
                        busy += 1
                        continue
                    try:  # clear the read-only flag and retry
                        os.chmod(entry.path, stat.S_IWRITE)
                        os.unlink(entry.path)
                    except OSError:
                        busy += 1
                        continue
                except OSError:
                    busy += 1
                    continue
            files += 1
            size += st.st_size
    removed = 0
    if not ctx.simulate:
        # Deepest first, so a folder emptied by removing its subfolders can go too.
        for path, modified in sorted(folders, key=lambda folder: folder[0].count(os.sep), reverse=True):
            if modified < cutoff:
                try:
                    os.rmdir(path)  # only succeeds on an empty folder
                    removed += 1
                except OSError:
                    pass
    return files, size, busy, removed


def step_clean_temp_files(ctx, cfg):
    days = float(cfg.get("older_than_days", 3))
    cutoff = time.time() - days * 86400
    roots = []
    for raw in for_this_os(cfg.get("paths"), []):
        path = ctx.expand(raw)
        if not path or not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in roots:
            continue
        if unsafe_root(real, ctx):
            ctx.log("refusing to clean %s: it is a system or home folder" % real, "warn")
            continue
        roots.append(real)
    if not roots:
        raise StepSkipped("none of the configured temp folders exist")
    total_files = total_size = total_busy = 0
    for root in roots:
        files, size, busy, folders = clean_tree(ctx, root, cutoff)
        total_files += files
        total_size += size
        total_busy += busy
        if files or busy:
            extra = ", %d in use" % busy if busy else ""
            extra += ", %d empty folder(s) removed" % folders if folders else ""
            ctx.log("%s: %d file(s), %s%s" % (root, files, fmt_bytes(size), extra))
    note = "; %d in use were left" % total_busy if total_busy else ""
    verb = "would delete" if ctx.simulate else "deleted"
    return "%s %d file(s) older than %s, %s%s" % (verb, total_files, fmt_age(days), fmt_bytes(total_size), note)


def step_flush_dns(ctx, cfg):
    if IS_WINDOWS:
        commands = [["ipconfig", "/flushdns"]]
    elif IS_MAC:
        if not ctx.is_admin:
            raise StepSkipped("needs root on macOS (add --elevate)")
        commands = [["dscacheutil", "-flushcache"], ["killall", "-HUP", "mDNSResponder"]]
    elif shutil.which("resolvectl") and run(["systemctl", "is-active", "--quiet", "systemd-resolved"])[0] == 0:
        commands = [["resolvectl", "flush-caches"]]
    elif shutil.which("nscd") and any(norm(p.info["name"]) == "nscd" for p in psutil.process_iter(["name"])):
        commands = [["nscd", "-i", "hosts"]]
    else:
        raise StepSkipped("no local DNS cache (systemd-resolved or nscd) is running")
    if ctx.simulate:
        return "would run: " + "; ".join(" ".join(command) for command in commands)
    for command in commands:
        code, out = run(command)
        if code:
            raise RuntimeError("%s failed: %s" % (" ".join(command), out.splitlines()[-1] if out else code))
    return "DNS cache flushed"


STEPS = {
    "kill_hung_apps": step_kill_hung_apps,
    "stop_background_apps": step_stop_background_apps,
    "prune_orphans": step_prune_orphans,
    "restart_shell": step_restart_shell,
    "trim_working_sets": step_trim_working_sets,
    "purge_memory_caches": step_purge_memory_caches,
    "clean_temp_files": step_clean_temp_files,
    "flush_dns": step_flush_dns,
}


# ---------------------------------------------------------------- reporting

def snapshot():
    """[(label, kind, value)] describing memory and process load right now."""
    memory = psutil.virtual_memory()
    rows = [("Available RAM", "bytes", memory.available)]
    perf = None
    if IS_WINDOWS:
        lists = win_memory_lists()
        for label, key in (("Free RAM", "free"), ("Standby cache", "standby"), ("Modified (dirty)", "modified")):
            if key in lists:
                rows.append((label, "bytes", lists[key]))
        perf = win_performance_info()
        if perf:
            rows.append(("Committed", "bytes", perf.CommitTotal * perf.PageSize))
    elif IS_MAC:
        rows += [("Free RAM", "bytes", memory.free), ("Inactive (cache)", "bytes", memory.inactive),
                 ("Wired", "bytes", memory.wired)]
    else:
        rows += [("Free RAM", "bytes", memory.free),
                 ("Page cache", "bytes", getattr(memory, "cached", 0) + getattr(memory, "buffers", 0))]
    rows.append(("Swap used", "bytes", psutil.swap_memory().used))
    if perf:
        rows += [("Processes", "count", perf.ProcessCount), ("Threads", "count", perf.ThreadCount),
                 ("Handles", "count", perf.HandleCount)]
    else:
        processes = threads = 0
        for proc in psutil.process_iter(["num_threads"]):
            processes += 1
            threads += proc.info["num_threads"] or 0
        rows += [("Processes", "count", processes), ("Threads", "count", threads)]
    return rows


def log_snapshot(log, before, after=None):
    if after is None:
        for label, kind, value in before:
            log("%-20s%12s" % (label, fmt_metric(kind, value)))
        return
    later = {label: value for label, _, value in after}
    log("%-20s%12s%12s%12s" % ("", "before", "after", "change"))
    for label, kind, value in before:
        if label in later:
            log("%-20s%12s%12s%12s" % (label, fmt_metric(kind, value), fmt_metric(kind, later[label]),
                                       fmt_metric(kind, later[label] - value, signed=True)))


def report_heavy_hitters(ctx):
    settings = ctx.config.get("report", {})
    top = int(settings.get("top_count", 8))
    heavy = float(settings.get("heavy_app_mb", 1500)) * MB
    leak = int(settings.get("handle_leak_threshold", 10000))
    groups, leaky = {}, []
    for proc in psutil.process_iter(["name"]):
        name = strip_exe(proc.info["name"] or "")
        if not name:
            continue
        group = groups.setdefault(norm(name), {"name": name, "count": 0, "memory": 0})
        group["count"] += 1
        group["memory"] += app_memory(proc)
        if IS_WINDOWS and not ctx.is_protected(name):
            try:
                handles = proc.num_handles()
            except psutil.Error:
                continue
            if handles >= leak:
                leaky.append((handles, name, proc.pid))
    ranked = sorted(groups.values(), key=lambda group: group["memory"], reverse=True)
    ctx.log("Heaviest apps now (%s memory)" % ("private" if IS_WINDOWS else "resident"), "head")
    for group in ranked[:top]:
        ctx.log("%-36s%11s  x%d" % (group["name"][:36], fmt_bytes(group["memory"]), group["count"]))
    for group in ranked:
        if group["memory"] < heavy:
            break
        if not ctx.is_protected(group["name"]):
            ctx.log("%s holds %s; closing or restarting it is the biggest win left"
                    % (group["name"], fmt_bytes(group["memory"])), "warn")
    for handles, name, pid in sorted(leaky, reverse=True):
        ctx.log("%s [%d] holds %s handles; a leak that size only clears when it restarts"
                % (name, pid, "{:,}".format(handles)), "warn")


# ---------------------------------------------------------------- elevation and scheduling

def relaunch_elevated(argv, script=SCRIPT_PATH):
    forwarded = [arg for arg in argv if arg != "--elevate"]
    if IS_WINDOWS:
        params = subprocess.list2cmdline([script] + forwarded + ["--pause"])
        result = _ShellExecuteW(None, "runas", sys.executable, params, None, 1) or 0
        if result <= 32:
            print("Elevation was cancelled or failed (code %d)." % result)
            return 1
        print("Continuing in the administrator window.")
        return 0
    # sudo resets the environment, so point root's Python at the psutil this user installed.
    command = ["sudo", "env", "PYTHONPATH=" + PSUTIL_DIR, sys.executable, script] + forwarded
    os.execvp("sudo", command)


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Runs system_refresh.py to prune resource bloat.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_windows(flags, minutes):
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    command = pythonw if os.path.exists(pythonw) else sys.executable
    domain = os.environ.get("USERDOMAIN")
    user = "%s\\%s" % (domain, getpass.getuser()) if domain else getpass.getuser()
    xml = TASK_XML.format(
        minutes=minutes,
        start=(datetime.now() + timedelta(minutes=5)).replace(microsecond=0).isoformat(),
        user=escape(user),
        run_level="HighestAvailable" if is_admin() else "LeastPrivilege",
        command=escape(command),
        arguments=escape(subprocess.list2cmdline([SCRIPT_PATH] + flags)),
        workdir=escape(SCRIPT_DIR))
    path = os.path.join(tempfile.gettempdir(), "system_refresh_task.xml")
    with open(path, "w", encoding="utf-16") as target:
        target.write(xml)
    try:
        code, out = run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", path])
    finally:
        os.remove(path)
    print(out)
    return code


def install_mac(flags, minutes):
    import plistlib
    as_root = os.geteuid() == 0
    path = ("/Library/LaunchDaemons/%s.plist" % LAUNCHD_LABEL if as_root
            else os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL))
    domain = "system" if as_root else "gui/%d" % os.getuid()
    job = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, SCRIPT_PATH] + flags,
        "EnvironmentVariables": {"PYTHONPATH": PSUTIL_DIR},
        "StartInterval": minutes * 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])  # replace an earlier install
    with open(path, "wb") as target:
        plistlib.dump(job, target)
    code, out = run(["launchctl", "bootstrap", domain, path])
    print(out or "Loaded %s into launchd (%s)." % (path, domain))
    return code


def install_cron(flags, minutes):
    if minutes < 60:
        schedule = "*/%d * * * *" % minutes
    elif minutes < 24 * 60:
        schedule = "0 */%d * * *" % max(1, round(minutes / 60))
    else:
        schedule = "0 3 */%d * *" % max(1, round(minutes / (24 * 60)))
    command = " ".join(shlex.quote(part) for part in [sys.executable, SCRIPT_PATH] + flags)
    line = "%s PYTHONPATH=%s %s >/dev/null 2>&1 %s" % (schedule, shlex.quote(PSUTIL_DIR), command, CRON_MARKER)
    code, current = run(["crontab", "-l"])
    lines = [kept for kept in current.splitlines() if CRON_MARKER not in kept] if code == 0 else []
    result = subprocess.run(["crontab", "-"], input="\n".join(lines + [line]) + "\n",
                            universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout.strip() or "Added to %s's crontab: %s" % (getpass.getuser(), schedule))
    return result.returncode


def install(args, config_path):
    flags = ["--quiet"]
    if args.deep:
        flags.append("--deep")
    if args.require_idle:
        flags += ["--require-idle", "%g" % args.require_idle]
    if config_path != DEFAULT_CONFIG_PATH:
        flags += ["--config", config_path]
    if not IS_WINDOWS and os.geteuid() == 0:
        target = args.target_user or os.environ.get("SUDO_USER")
        if target:
            flags += ["--target-user", target]
    minutes = max(1, int(round(args.every * 60)))
    if IS_WINDOWS:
        return install_windows(flags, minutes)
    if IS_MAC:
        return install_mac(flags, minutes)
    return install_cron(flags, minutes)


def uninstall():
    if IS_WINDOWS:
        if run(["schtasks", "/Query", "/TN", TASK_NAME])[0] != 0:
            print("No scheduled run was installed.")
            return 0
        code, out = run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        print(out)
        return code
    if IS_MAC:
        as_root = os.geteuid() == 0
        domain = "system" if as_root else "gui/%d" % os.getuid()
        path = ("/Library/LaunchDaemons/%s.plist" % LAUNCHD_LABEL if as_root
                else os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL))
        run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])
        if os.path.exists(path):
            os.remove(path)
            print("Removed %s." % path)
        else:
            print("No scheduled run was installed.")
        return 0
    code, current = run(["crontab", "-l"])
    lines = current.splitlines() if code == 0 else []
    kept = [line for line in lines if CRON_MARKER not in line]
    if len(kept) == len(lines):
        print("No scheduled run was installed.")
        return 0
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n" if kept else "", universal_newlines=True)
    print("Removed the system_refresh line from the crontab.")
    return 0


# ---------------------------------------------------------------- main

def step_list(text):
    return [step.strip() for step in text.split(",") if step.strip()]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Prune resource bloat on a running machine; the next best thing to a reboot.",
        epilog="Steps, in run order: " + ", ".join(STEP_ORDER))
    parser.add_argument("--deep", action="store_true", help='also run the steps whose mode is "deep"')
    parser.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    parser.add_argument("--only", type=step_list, default=[], metavar="STEPS",
                        help="run just these comma-separated steps, whatever their mode")
    parser.add_argument("--skip", type=step_list, default=[], metavar="STEPS", help="never run these steps")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="settings file (default: %(default)s)")
    parser.add_argument("--elevate", action="store_true",
                        help="relaunch as administrator (UAC prompt) or root (sudo) for the admin-only steps")
    parser.add_argument("--require-idle", type=float, default=0, metavar="MINUTES",
                        help="do nothing unless there has been no keyboard or mouse input for this long "
                             "(Windows and macOS)")
    parser.add_argument("--target-user", metavar="USER",
                        help="when running as root: whose processes and temp files to clean (default: $SUDO_USER)")
    parser.add_argument("--quiet", action="store_true", help="write to the log file only")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    schedule = parser.add_argument_group("scheduling")
    schedule.add_argument("--install", action="store_true",
                          help="run automatically from Task Scheduler, launchd or cron; combine with "
                               "--deep, --require-idle and --config to choose how")
    schedule.add_argument("--every", type=float, default=4, metavar="HOURS",
                          help="interval for --install (default: 4)")
    schedule.add_argument("--uninstall", action="store_true", help="remove the scheduled run")
    args = parser.parse_args(argv)
    unknown = [step for step in args.only + args.skip if step not in STEP_ORDER]
    if unknown:
        parser.error("unknown step(s): %s" % ", ".join(unknown))
    return args


def pause_if_asked(args):
    if args.pause:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass


def main():
    argv = sys.argv[1:]
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # window titles may not fit the console's code page
    if args.elevate and not is_admin():
        return relaunch_elevated(argv)
    if args.uninstall:
        code = uninstall()
        pause_if_asked(args)
        return code
    config_path = os.path.abspath(args.config)
    try:
        config, created = load_config(config_path)
    except (OSError, ValueError) as error:
        print("Could not read %s: %s" % (config_path, error))
        return 2
    if args.install:
        code = install(args, config_path)
        pause_if_asked(args)
        return code

    log = Log(args.quiet)
    log_dir = os.path.join(SCRIPT_DIR, os.path.expandvars(os.path.expanduser(config.get("log_directory", "logs"))))
    os.makedirs(log_dir, exist_ok=True)
    log.open(os.path.join(log_dir, "refresh-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S")))
    old_logs = sorted(name for name in os.listdir(log_dir) if name.startswith("refresh-") and name.endswith(".log"))
    for name in old_logs[:-max(1, int(config.get("keep_logs", 30)))]:
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            pass

    try:
        ctx = Context(args, config, log)
    except KeyError:
        print("No such user: %s" % (args.target_user or os.environ.get("SUDO_USER")))
        return 2
    for path in (log_dir, log.path) + ((config_path,) if created else ()):
        ctx.give_back(path)
    started = time.time()
    mode = "only " + ", ".join(args.only) if args.only else "deep" if args.deep else "standard"
    who = ("administrator" if IS_WINDOWS else "root") if ctx.is_admin else "standard user"
    if not IS_WINDOWS and ctx.is_admin and ctx.user != "root":
        who += ", acting for " + ctx.user
    log("System refresh on %s %s: %s%s, %s" % (OS_NAME, platform.release(), mode,
                                               " (dry run, nothing will change)" if ctx.simulate else "", who), "head")
    if created:
        log("wrote default settings to %s" % config_path)
    for name in config.get("steps", {}):
        if name not in STEPS:
            log("ignoring unknown step '%s' in %s" % (name, config_path), "warn")

    if args.require_idle:
        idle = idle_seconds()
        if idle is None:
            log("can't measure idle time on %s; running anyway" % OS_NAME, "warn")
        elif idle < args.require_idle * 60:
            log("last input %.0f s ago; waiting for %g idle minutes, so nothing was done" % (idle, args.require_idle),
                "skip")
            return 0

    before = snapshot()
    results, deep_only = [], []
    for name in STEP_ORDER:
        cfg = config["steps"].get(name, {})
        step_mode = str(cfg.get("mode", "off")).lower()
        if step_mode not in ("on", "deep", "off"):
            log('%s: unknown mode "%s"; treating it as "off"' % (name, step_mode), "warn")
            step_mode = "off"
        if args.only:
            selected = name in args.only
        else:
            selected = name not in args.skip and (step_mode == "on" or (step_mode == "deep" and args.deep))
        if not selected:
            if step_mode == "deep" and not args.deep and not args.only and name not in args.skip:
                deep_only.append(name)
            continue
        log(name, "head")
        try:
            outcome, level = STEPS[name](ctx, cfg), "ok"
        except StepSkipped as reason:
            outcome, level = "skipped: %s" % reason, "skip"
        except Exception as error:  # one failing step must not stop the rest
            outcome, level = "failed: %s" % error, "warn"
        log(outcome, level)
        results.append((name, outcome, level))

    log("Summary", "head")
    for name, outcome, level in results:
        log("%-22s%s" % (name, outcome), level)
    if deep_only:
        log("not run without --deep: %s" % ", ".join(deep_only), "skip")

    log("Memory and load", "head")
    if ctx.simulate:
        log_snapshot(log, before)
    else:
        time.sleep(2)  # let the counters settle
        log_snapshot(log, before, snapshot())
    report_heavy_hitters(ctx)
    log("finished in %.1f s; log: %s" % (time.time() - started, log.path), "head")
    pause_if_asked(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
