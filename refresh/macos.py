"""macOS: how system_refresh reaches each goal on a Mac."""

import ctypes
import os
import plistlib
import re
import shutil
import time

import psutil

from . import common
from .common import Strategy, fmt_bytes, norm, run

_APP_HEADER = re.compile(r'^\s*\d+\) "(.*)" ASN:')
_APP_PID = re.compile(r'pid = (\d+) type="(\w+)"')
_APP_EXE = re.compile(r'executable path="(.*)"')
SYSTEM_PREFIXES = ("/System/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/usr/lib/", "/bin/", "/sbin/",
                   "/Library/Apple/", "/private/var/db/")
# The desktop shell launches everything, so being a Dock app doesn't make it an app's parent.
SHELLS = ["finder", "dock", "launchd", "loginwindow", "windowserver", "systemuiserver", "controlcenter"]


# ---------------------------------------------------------------- Mac helpers

def as_user(ctx, command):
    """Runs a command in the user's own session, even when this run is root under sudo."""
    if os.geteuid() == 0 and ctx.uid:
        command = ["launchctl", "asuser", str(ctx.uid)] + command
    return run(command)


def ls_apps(ctx):
    """Apps LaunchServices knows about: [{"name", "pid", "type", "exe"}]. type is Foreground (Dock apps),
    UIElement (menu bar apps) or BackgroundOnly."""
    _, out = as_user(ctx, ["lsappinfo", "list"])
    apps, current = [], None
    for line in out.splitlines():
        header = _APP_HEADER.match(line)
        if header:
            current = {"name": header.group(1), "pid": None, "type": "", "exe": ""}
            apps.append(current)
            continue
        if current is None:
            continue
        found = _APP_PID.search(line)
        if found:
            current["pid"], current["type"] = int(found.group(1)), found.group(2)
        found = _APP_EXE.search(line)
        if found:
            current["exe"] = found.group(1)
    return [app for app in apps if app["pid"]]


def launchd_job_pids(ctx):
    """PIDs launchd started on purpose (agents, daemons, apps); they look orphaned but aren't."""
    _, out = as_user(ctx, ["launchctl", "list"])
    pids = set()
    for line in out.splitlines()[1:]:
        first = line.split(None, 1)[0] if line.strip() else ""
        if first.isdigit():
            pids.add(int(first))
    return pids


def launch_agents(ctx):
    """Executable path -> label for the launch agents that start with a login."""
    found = {}
    for folder in (os.path.join(ctx.home, "Library/LaunchAgents"), "/Library/LaunchAgents"):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".plist"):
                continue
            try:
                with open(os.path.join(folder, name), "rb") as source:
                    job = plistlib.load(source)
                program = job.get("Program") or (job.get("ProgramArguments") or [None])[0]
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if program:
                found[program] = "launch agent %s" % job.get("Label", name[:-len(".plist")])
    return found


def started_with_nohup(pid):
    """True if the process ignores SIGHUP, as nohup makes it; None when ps can't tell."""
    for keyword in ("ignored", "sigignore"):  # macOS's ps names the field "ignored"; other BSDs also "sigignore"
        code, out = run(["ps", "-o", keyword + "=", "-p", str(pid)])
        if code == 0 and out.strip():
            try:
                return bool(int(out.strip(), 16) & 1)  # bit 0 is SIGHUP
            except ValueError:
                return None
    return None


class _PSN(ctypes.Structure):
    _fields_ = [("high", ctypes.c_uint32), ("low", ctypes.c_uint32)]


_UNRESPONSIVE = []  # memo: [check function, or None when unavailable]


def _symbol(library, *names):
    for name in names:
        try:
            return getattr(library, name)
        except AttributeError:
            pass
    return None


def unresponsive_check():
    """The window server's own "Not Responding" flag, the one Force Quit and Activity Monitor show.
    It's an undocumented call, so this is None when macOS doesn't expose it or there's no desktop session."""
    if _UNRESPONSIVE:
        return _UNRESPONSIVE[0]
    check = None
    _, manager = run(["launchctl", "managername"])
    if manager.strip() == "Aqua":
        connection_fn = unresponsive_fn = process_fn = None
        for path in ("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
                     "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
                     "/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight"):
            try:
                library = ctypes.CDLL(path)
            except OSError:
                continue
            connection_fn = connection_fn or _symbol(library, "CGSMainConnectionID", "SLSMainConnectionID")
            unresponsive_fn = unresponsive_fn or _symbol(library, "CGSEventIsAppUnresponsive",
                                                         "SLSEventIsAppUnresponsive")
            process_fn = process_fn or _symbol(library, "GetProcessForPID")
        if connection_fn and unresponsive_fn and process_fn:
            connection_fn.restype, connection_fn.argtypes = ctypes.c_int, []
            unresponsive_fn.restype = ctypes.c_bool
            unresponsive_fn.argtypes = [ctypes.c_int, ctypes.POINTER(_PSN)]
            process_fn.restype = ctypes.c_int32
            process_fn.argtypes = [ctypes.c_int, ctypes.POINTER(_PSN)]
            connection = connection_fn()
            if connection:
                def check(pid):
                    psn = _PSN()
                    if process_fn(pid, ctypes.byref(psn)) != 0:
                        return False
                    return bool(unresponsive_fn(connection, ctypes.byref(psn)))
    _UNRESPONSIVE.append(check)
    return check


def unresponsive_apps(ctx):
    """pid -> app name for apps the window server currently marks Not Responding."""
    check = unresponsive_check()
    if not check:
        return {}
    return {app["pid"]: app["name"] for app in ls_apps(ctx) if app["type"] != "BackgroundOnly" and check(app["pid"])}


def user_memory(ctx):
    total = 0
    for proc in psutil.process_iter(["uids", "memory_info"]):
        uids, info = proc.info["uids"], proc.info["memory_info"]
        if uids and info and uids.real == ctx.uid:
            total += info.rss
    return total


def idle_seconds():
    _, out = run(["ioreg", "-c", "IOHIDSystem", "-d", "4"])
    for line in out.splitlines():
        if '"HIDIdleTime"' in line:
            try:
                return int(line.rsplit("=", 1)[1].strip()) / 1e9
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- facts the shared code asks for

UNSUPPORTED = {}


def prepare(ctx):
    if os.geteuid() == 0 or not os.environ.get("TMPDIR"):
        code, out = as_user(ctx, ["getconf", "DARWIN_USER_TEMP_DIR"])
        if code == 0 and out:
            os.environ["TMPDIR"] = out


def owns(ctx, proc):
    return proc.uids().real == ctx.uid


def is_orphan(proc, procs):
    return proc.info["ppid"] == 1


def orphan_keeper(ctx):
    jobs = launchd_job_pids(ctx)

    def keep(node, name):
        return "%s is a launchd job" % name if node.pid in jobs else None
    return keep


def ui_pids(ctx, procs):
    return {app["pid"] for app in ls_apps(ctx) if app["type"] == "Foreground"}


def discovery_classifier(ctx):
    """classify(proc, name, exe, parent_name) -> (why it isn't a candidate or None, [labels])."""
    apps = {app["pid"]: app for app in ls_apps(ctx)}
    agents = launch_agents(ctx)
    kinds = {"UIElement": "menu bar app", "BackgroundOnly": "background app"}

    def classify(proc, name, exe, parent_name):
        app = apps.get(proc.pid)
        if exe.startswith(SYSTEM_PREFIXES):
            return "part of macOS", []
        try:
            if proc.terminal():
                return "started from a terminal", []
        except psutil.Error:
            pass
        labels = [label for label in (agents.get(exe), kinds.get(app["type"]) if app else None) if label]
        return None, labels or ["runs in the background"]
    return classify


def memory_rows(memory):
    return [("Free RAM", "bytes", memory.free), ("Inactive (cache)", "bytes", memory.inactive),
            ("Wired", "bytes", memory.wired)]


def load_rows():
    return None


def enable_ansi():
    return True


# ---------------------------------------------------------------- ways to reach each goal

class UnresponsiveApps(Strategy):
    goal = "kill_hung_apps"
    label = "end apps macOS marks Not Responding (undocumented window server call)"

    def unavailable(self, ctx):
        if unresponsive_check() is None:
            return "can't read the Not Responding flag here (needs a desktop session on a macOS that exposes it)"
        return None

    def run(self, ctx, cfg):
        return common.end_frozen_apps(ctx, cfg, lambda: unresponsive_apps(ctx))


class RestartFinder(Strategy):
    goal = "restart_shell"
    label = "restart Finder once it has bloated"

    def run(self, ctx, cfg):
        # macOS relaunches Finder itself; only start it by hand when not root, so it never runs as root.
        relaunch = None if ctx.is_admin else ["open", "-a", "Finder"]
        return common.restart_shell_process(ctx, cfg, "finder", "Finder", relaunch)


class MemoryPressureWarning(Strategy):
    goal = "release_idle_memory"
    label = "send apps a low-memory warning so they shed caches (memory_pressure)"
    needs_admin = True
    deep_only = True

    def unavailable(self, ctx):
        return None if shutil.which("memory_pressure") else "the memory_pressure tool isn't installed"

    def run(self, ctx, cfg):
        level = str(cfg.get("pressure_level", "warn"))
        if level not in ("warn", "critical"):
            level = "warn"
        seconds = int(cfg.get("pressure_seconds", 5))
        if ctx.simulate:
            return "would send apps a %s-level low-memory warning for %d s" % (level, seconds)
        before = user_memory(ctx)
        code, out = run(["memory_pressure", "-S", "-l", level, "-s", str(seconds)], timeout=seconds + 120)
        if code:
            raise RuntimeError("memory_pressure failed: %s" % (out.splitlines()[-1] if out else code))
        time.sleep(2)
        after = user_memory(ctx)
        return "sent a %s-level low-memory warning; your apps went from %s to %s" % (
            level, fmt_bytes(before), fmt_bytes(after))


class Purge(Strategy):
    goal = "purge_memory_caches"
    label = "sync disks and purge the disk cache (purge)"
    needs_admin = True

    def unavailable(self, ctx):
        return None if shutil.which("purge") else "the purge tool isn't installed"

    def run(self, ctx, cfg):
        if ctx.simulate:
            return "would sync disks and purge the disk cache"
        os.sync()
        code, out = run(["purge"], timeout=300)
        if code:
            raise RuntimeError("purge failed: %s" % out)
        return "disk cache purged"


class DnsFlush(Strategy):
    goal = "flush_dns"
    label = "dscacheutil -flushcache and restart mDNSResponder"
    needs_admin = True

    def run(self, ctx, cfg):
        commands = [["dscacheutil", "-flushcache"], ["killall", "-HUP", "mDNSResponder"]]
        if ctx.simulate:
            return "would run: " + "; ".join(" ".join(command) for command in commands)
        for command in commands:
            code, out = run(command)
            if code:
                raise RuntimeError("%s failed: %s" % (" ".join(command), out.splitlines()[-1] if out else code))
        return "DNS cache flushed"


STRATEGIES = [UnresponsiveApps(), RestartFinder(), MemoryPressureWarning(), Purge(), DnsFlush()]
