"""Windows: how system_refresh reaches each goal on Windows, through Win32 calls made with ctypes."""

import ctypes
import os
from ctypes import wintypes

import psutil

from . import common
from .common import Strategy, fmt_bytes, norm, run, strip_exe, MB

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

# Processes started from these are someone's interactive work, not background helpers.
TERMINALS = {"cmd", "powershell", "pwsh", "windowsterminal", "openconsole", "conhost", "bash", "wsl", "wslhost"}
# The desktop shell launches everything, so having a window doesn't make it an app's parent.
SHELLS = ["explorer", "applicationframehost", "sihost", "startmenuexperiencehost", "shellexperiencehost"]
# Parts of Windows that live outside the Windows folder.
SYSTEM_FOLDERS = [r"%WINDIR%", r"%ProgramFiles%\Common Files\microsoft shared", r"%ProgramFiles%\Windows Defender",
                  r"%ProgramData%\Microsoft\Windows Defender"]


# ---------------------------------------------------------------- Win32 helpers

def session_id(pid):
    session = wintypes.DWORD()
    return session.value if _ProcessIdToSessionId(pid, ctypes.byref(session)) else None


def top_windows():
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


def hung_windows():
    """pid -> window title for apps whose top-level window has stopped responding."""
    hung = {}
    for hwnd, pid in top_windows():
        if pid not in hung and _IsHungAppWindow(hwnd):
            # GetWindowText reads another process's caption without messaging it, so it can't hang here.
            title = ctypes.create_unicode_buffer(256)
            _GetWindowTextW(hwnd, title, 256)
            hung[pid] = title.value
    return hung


def foreground_pid():
    pid = wintypes.DWORD()
    hwnd = _GetForegroundWindow()
    if hwnd:
        _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def trim(pid, check_only=False):
    """Empties a process's working set; the pages stay in RAM on the standby or modified list."""
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
    if not handle:
        return False
    try:
        return check_only or bool(_EmptyWorkingSet(handle))
    finally:
        _CloseHandle(handle)


def enable_privilege(name):
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


def flush_file_cache():
    """Shrinks the system file cache to nothing; returns 0 or a Win32 error code."""
    error = enable_privilege("SeIncreaseQuotaPrivilege")
    if error:
        return error
    unlimited = ctypes.c_size_t(-1).value
    return 0 if _SetSystemFileCacheSize(unlimited, unlimited, 0) else ctypes.get_last_error()


def memory_list_command(command):
    """Runs a SystemMemoryListInformation command; returns an NTSTATUS (0 = success)."""
    if enable_privilege("SeProfileSingleProcessPrivilege"):
        return STATUS_PRIVILEGE_NOT_HELD
    value = ctypes.c_int(command)
    return _NtSetSystemInformation(80, ctypes.byref(value), ctypes.sizeof(value)) & 0xFFFFFFFF


def performance_info():
    info = _PERFORMANCE_INFORMATION(cb=ctypes.sizeof(_PERFORMANCE_INFORMATION))
    return info if _GetPerformanceInfo(ctypes.byref(info), info.cb) else None


def memory_lists():
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


def idle_seconds():
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _GetLastInputInfo(ctypes.byref(info)):
        return None
    return ((_GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def enable_ansi():
    handle = _GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))  # STD_OUTPUT_HANDLE
    mode = wintypes.DWORD()
    if not _GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(_SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def console_input():
    """True when someone can type answers: Windows calls the NUL device a terminal too."""
    mode = wintypes.DWORD()
    return bool(_GetConsoleMode(_GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF)), ctypes.byref(mode)))


def run_as_admin(executable, params):
    """Starts a program through the UAC prompt; returns ShellExecute's code (above 32 = started)."""
    return _ShellExecuteW(None, "runas", executable, params, None, 1) or 0


# ---------------------------------------------------------------- facts the shared code asks for

UNSUPPORTED = {}


def prepare(ctx):
    ctx.session = session_id(os.getpid())


def owns(ctx, proc):
    return session_id(proc.pid) == ctx.session


def is_orphan(proc, procs):
    # Windows keeps a dead parent's PID: the parent is gone, or the PID now names a younger process.
    parent = procs.get(proc.info["ppid"])
    return parent is None or (parent.info["create_time"] or 0) > proc.info["create_time"]


def orphan_keeper(ctx):
    windowed = {pid for _, pid in top_windows()}

    def keep(node, name):
        return "%s has a window open" % name if node.pid in windowed else None
    return keep


def started_with_nohup(pid):
    return False


def startup_items():
    """Executable path or app name (lowercase) -> label, for programs that start at login."""
    import winreg
    items = {}
    keys = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")]
    for hive, path in keys:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            index = 0
            while True:
                try:
                    _, command, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                exe = command_exe(str(command))
                if exe:
                    items[os.path.normcase(exe)] = "starts at login"
    for folder in (os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"),
                   os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs\StartUp")):
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            items[norm(os.path.splitext(entry)[0])] = "starts at login"
    return items


def command_exe(command):
    """The program a Run-key command line starts."""
    command = os.path.expandvars(command.strip())
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    end = command.lower().find(".exe")
    return command[:end + 4] if end >= 0 else command.split(" ", 1)[0]


def ui_pids(ctx, procs):
    return {pid for _, pid in top_windows()}


def discovery_classifier(ctx):
    """classify(proc, name, exe, parent_name) -> (why it isn't a candidate or None, [labels])."""
    startup = startup_items()
    system = tuple(os.path.normcase(os.path.expandvars(folder)) + os.sep for folder in SYSTEM_FOLDERS
                   if "%" not in os.path.expandvars(folder))

    def classify(proc, name, exe, parent_name):
        if exe and os.path.normcase(exe).startswith(system):
            return "part of Windows", []
        if norm(parent_name) in TERMINALS:
            return "started from a terminal", []
        label = startup.get(os.path.normcase(exe)) if exe else None
        return None, [label or startup.get(norm(name), "runs in the background")]
    return classify


def memory_rows(memory):
    rows = []
    lists = memory_lists()
    for label, key in (("Free RAM", "free"), ("Standby cache", "standby"), ("Modified (dirty)", "modified")):
        if key in lists:
            rows.append((label, "bytes", lists[key]))
    perf = performance_info()
    if perf:
        rows.append(("Committed", "bytes", perf.CommitTotal * perf.PageSize))
    return rows


def load_rows():
    perf = performance_info()
    if not perf:
        return None
    return [("Processes", "count", perf.ProcessCount), ("Threads", "count", perf.ThreadCount),
            ("Handles", "count", perf.HandleCount)]


# ---------------------------------------------------------------- ways to reach each goal

class HungWindows(Strategy):
    goal = "kill_hung_apps"
    label = "end apps whose window Windows marks Not Responding"

    def run(self, ctx, cfg):
        return common.end_frozen_apps(ctx, cfg, hung_windows)


class RestartExplorer(Strategy):
    goal = "restart_shell"
    label = "restart Explorer once it has bloated"

    def unavailable(self, ctx):
        if ctx.is_admin:
            return "from an elevated run Explorer would come back elevated; run without --elevate"
        return None

    def run(self, ctx, cfg):
        return common.restart_shell_process(ctx, cfg, "explorer", "Explorer", ["explorer.exe"])


class TrimWorkingSets(Strategy):
    goal = "release_idle_memory"
    label = "trim background apps' working sets"

    def run(self, ctx, cfg):
        minimum = float(cfg.get("min_working_set_mb", 100)) * MB
        foreground = None
        if cfg.get("skip_foreground_app", True):
            pid = foreground_pid()
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
            if not trim(proc.pid, check_only=ctx.simulate):
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


class PurgeStandby(Strategy):
    goal = "purge_memory_caches"
    label = "flush the file cache and modified list, purge the standby list"
    needs_admin = True

    def run(self, ctx, cfg):
        flush_modified = cfg.get("flush_modified_list", True)
        if ctx.simulate:
            plan = ["flush the file cache"] + (["write out the modified list"] if flush_modified else [])
            return "would " + ", ".join(plan + ["purge the standby list"])
        done = []
        error = flush_file_cache()
        if error:
            ctx.log("file cache flush failed (Win32 error %d)" % error, "warn")
        else:
            done.append("file cache flushed")
        commands = [(MEMORY_PURGE_STANDBY_LIST, "standby list purge", "standby list purged")]
        if flush_modified:
            commands.insert(0, (MEMORY_FLUSH_MODIFIED_LIST, "modified list flush", "modified list written out"))
        for command, action, outcome in commands:
            status = memory_list_command(command)
            if status:
                ctx.log("%s failed (NTSTATUS 0x%08X)" % (action, status), "warn")
            else:
                done.append(outcome)
        return ", ".join(done) or "nothing was purged"


class IpconfigFlush(Strategy):
    goal = "flush_dns"
    label = "ipconfig /flushdns"

    def run(self, ctx, cfg):
        if ctx.simulate:
            return "would run: ipconfig /flushdns"
        code, out = run(["ipconfig", "/flushdns"])
        if code:
            raise RuntimeError("ipconfig /flushdns failed: %s" % (out.splitlines()[-1] if out else code))
        return "DNS cache flushed"


STRATEGIES = [HungWindows(), RestartExplorer(), TrimWorkingSets(), PurgeStandby(), IpconfigFlush()]
