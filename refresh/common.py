"""Shared building blocks: host facts, settings, logging, the run context and process helpers."""

import fnmatch
import getpass
import importlib
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

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
ADMIN = "administrator" if IS_WINDOWS else "root"

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.dirname(PACKAGE_DIR)
SCRIPT_PATH = os.path.join(SCRIPT_DIR, "system_refresh.py")
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "refresh_config.json")
PSUTIL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(psutil.__file__)))
MB = 1024 * 1024
GB = 1024 * MB

STEP_ORDER = [
    "kill_hung_apps", "stop_background_apps", "prune_orphans", "restart_shell",
    "release_idle_memory", "purge_memory_caches", "clean_temp_files", "flush_dns",
]
RENAMED_STEPS = {"trim_working_sets": "release_idle_memory"}

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
        "release_idle_memory": {
            "mode": "on",
            "min_working_set_mb": 100,
            "skip_foreground_app": True,
            "pressure_level": "warn",
            "pressure_seconds": 5,
            "reclaim_percent": 10,
        },
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
    """A goal that cannot or should not be worked on here; the message says why."""


class Strategy(object):
    """One way of reaching a goal. goals.py and the platform modules define the real ones."""

    goal = None         # which entry of STEP_ORDER this works toward
    label = ""          # what it does, shown in logs and --explain
    needs_admin = False
    deep_only = False   # heavier than the goal's other ways: runs only with --deep

    def unavailable(self, ctx):
        """Why this can't work on this machine, or None if it can."""
        return None

    def run(self, ctx, cfg):
        raise NotImplementedError


def platform_module():
    """refresh.windows, refresh.macos or refresh.linux: this OS's ways of reaching each goal."""
    name = {"windows": "windows", "darwin": "macos", "linux": "linux"}[OS_KEY]
    return importlib.import_module("." + name, __package__)


def os_label():
    if IS_MAC:
        return "macOS %s" % (platform.mac_ver()[0] or platform.release())
    if IS_WINDOWS:
        return "Windows %s (build %s)" % (platform.release(), platform.version().split(".")[-1])
    try:
        with open("/etc/os-release") as release:
            for line in release:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux %s" % platform.release()


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


def stdin_is_terminal():
    """True when someone can type answers at a prompt."""
    if not (sys.stdin and sys.stdin.isatty()):
        return False
    return platform_module().console_input() if IS_WINDOWS else True


def is_admin():
    if IS_WINDOWS:
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
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
            self.color = platform_module().enable_ansi()

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
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as target:
            json.dump(DEFAULT_CONFIG, target, indent=2)
            target.write("\n")
        return config, True
    with open(path, encoding="utf-8-sig") as source:
        user = json.load(source)
    steps = user.get("steps", {})
    for old, new in RENAMED_STEPS.items():
        if old in steps and new not in steps:
            steps[new] = steps.pop(old)
    deep_merge(config, user)
    return config, False


class Context(object):
    """Settings, identity and this OS's module, shared by every step."""

    def __init__(self, args, config, log):
        self.args = args
        self.config = config
        self.log = log
        self.simulate = bool(getattr(args, "dry_run", False) or config.get("dry_run"))
        # Naming a goal with --only is as deliberate as --deep, so its heavier ways may run too.
        self.deep = bool(getattr(args, "deep", False) or getattr(args, "only", None))
        self.is_admin = is_admin()
        self.untouchable = own_ancestry()
        extra = [norm(name) for name in for_this_os(config.get("protected_processes"), [])]
        self.protected = PROTECTED[OS_KEY] + extra
        self.session = self.uid = self.gid = None
        if IS_WINDOWS:
            self.user = getpass.getuser()
            self.home = os.path.expanduser("~")
        else:
            import pwd
            self.user = getattr(args, "target_user", None) or os.environ.get("SUDO_USER") or getpass.getuser()
            entry = pwd.getpwnam(self.user)
            self.uid, self.gid, self.home = entry.pw_uid, entry.pw_gid, entry.pw_dir
        self.platform = platform_module()
        self.platform.prepare(self)

    def is_protected(self, name):
        return matches(norm(name), self.protected)

    def owns(self, proc):
        """True for processes in this desktop session (Windows) or owned by the target user (Unix)."""
        try:
            return self.platform.owns(self, proc)
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


# ---------------------------------------------------------------- acting on processes

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


def end_frozen_apps(ctx, cfg, find_frozen):
    """Ends the apps find_frozen() reports ({pid: title}) if they are still frozen a few seconds later."""
    hung = []
    for pid, title in find_frozen().items():
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
    still_hung = find_frozen()
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


def restart_shell_process(ctx, cfg, shell, label, relaunch):
    """Restarts the desktop shell (Explorer, Finder) once it has outgrown cfg's limit."""

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
    if not shell_processes() and relaunch:
        subprocess.Popen(relaunch)
        time.sleep(3)
    after = sum(app_memory(p) for p in shell_processes())
    return "restarted %s: %s -> %s" % (label, fmt_bytes(before), fmt_bytes(after))
