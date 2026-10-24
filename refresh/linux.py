"""Linux: how system_refresh reaches each goal on Linux (written for RHEL 8 and later)."""

import errno
import os
import platform
import shlex
import shutil

import psutil

from .common import Strategy, fmt_bytes, norm, run

SYSTEM_PREFIXES = ("/usr/lib/systemd/", "/usr/sbin/", "/sbin/", "/usr/lib/polkit-1/")
# The desktop shell launches everything, so having a window doesn't make it an app's parent.
SHELLS = ["gnome-shell", "gnome-session*", "plasmashell", "kwin*", "systemd", "xfce4-*", "mate-panel", "cinnamon"]
CGROUP_ROOT = "/sys/fs/cgroup"


# ---------------------------------------------------------------- Linux helpers

def systemd_unit(pid):
    """The leaf of a process's systemd cgroup, e.g. "nginx.service" or "session-3.scope"."""
    try:
        with open("/proc/%d/cgroup" % pid) as cgroups:
            for line in cgroups:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[1] in ("", "name=systemd"):
                    return parts[2].rstrip("/").rsplit("/", 1)[-1]
    except OSError:
        pass
    return ""


def started_with_nohup(pid):
    try:
        with open("/proc/%d/status" % pid) as status:
            for line in status:
                if line.startswith("SigIgn:"):
                    return bool(int(line.split()[1], 16) & 1)  # bit 0 is SIGHUP
    except (OSError, ValueError):
        pass
    return None  # couldn't read it, so the orphan is kept


def desktop_entries(folders):
    """(program name, hidden) for every .desktop file in the folders."""
    entries = []
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".desktop"):
                continue
            command, hidden = None, False
            try:
                with open(os.path.join(folder, name), errors="replace") as source:
                    for line in source:
                        line = line.strip()
                        if line.startswith("Exec=") and command is None:
                            command = line[len("Exec="):]
                        elif line in ("NoDisplay=true", "Hidden=true"):
                            hidden = True
            except OSError:
                continue
            program = exec_program(command or "")
            if program:
                entries.append((program, hidden))
    return entries


def exec_program(command):
    """The program a .desktop Exec line starts, skipping env and VAR=value prefixes."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    while parts and (parts[0] == "env" or "=" in parts[0]):
        parts.pop(0)
    return norm(os.path.basename(parts[0])) if parts else ""


def user_slice(ctx):
    return os.path.join(CGROUP_ROOT, "user.slice", "user-%d.slice" % ctx.uid)


# ---------------------------------------------------------------- facts the shared code asks for

UNSUPPORTED = {
    "kill_hung_apps": "Linux desktops show their own 'not responding' prompt; there's no reliable way to check from outside",
    "restart_shell": "Linux desktop shells can't be restarted safely from outside",
}


def prepare(ctx):
    pass


def owns(ctx, proc):
    return proc.uids().real == ctx.uid


def is_orphan(proc, procs):
    if proc.info["ppid"] == 1:
        return True
    # Linux hands orphans to the nearest subreaper, normally `systemd --user`.
    parent = procs.get(proc.info["ppid"])
    return parent is not None and norm(parent.info["name"]) == "systemd"


def orphan_keeper(ctx):
    def keep(node, name):
        unit = systemd_unit(node.pid)
        return "%s runs as %s" % (name, unit) if unit.endswith(".service") else None
    return keep


def autostart_programs(ctx):
    return {program for program, hidden in desktop_entries(
        [os.path.join(ctx.home, ".config/autostart"), "/etc/xdg/autostart"]) if not hidden}


def ui_pids(ctx, procs):
    """Processes of desktop apps (those with a visible .desktop launcher), unless they start at login."""
    apps = {program for program, hidden in desktop_entries(
        ["/usr/share/applications", "/usr/local/share/applications",
         os.path.join(ctx.home, ".local/share/applications"), "/var/lib/flatpak/exports/share/applications"])
        if not hidden} - autostart_programs(ctx)
    return {proc.pid for proc in procs if norm(proc.info["name"]) in apps}


def discovery_classifier(ctx):
    """classify(proc, name, exe, parent_name) -> (why it isn't a candidate or None, [labels])."""
    autostart = autostart_programs(ctx)

    def classify(proc, name, exe, parent_name):
        if exe.startswith(SYSTEM_PREFIXES):
            return "part of the system", []
        try:
            if proc.terminal():
                return "started from a terminal", []
        except psutil.Error:
            pass
        unit = systemd_unit(proc.pid)
        if unit.endswith(".service"):
            return "managed by systemd (%s)" % unit, []
        return None, ["starts at login (autostart)" if norm(name) in autostart else "runs in the background"]
    return classify


def memory_rows(memory):
    return [("Free RAM", "bytes", memory.free),
            ("Page cache", "bytes", getattr(memory, "cached", 0) + getattr(memory, "buffers", 0))]


def load_rows():
    return None


def idle_seconds():
    return None


def enable_ansi():
    return True


# ---------------------------------------------------------------- ways to reach each goal

class CgroupReclaim(Strategy):
    goal = "release_idle_memory"
    label = "ask the kernel to reclaim idle memory from your apps (cgroup memory.reclaim)"
    needs_admin = True
    deep_only = True

    def unavailable(self, ctx):
        if not os.path.exists(os.path.join(CGROUP_ROOT, "cgroup.controllers")):
            return "needs cgroup v2 (this system uses cgroup v1)"
        if not os.path.exists(os.path.join(user_slice(ctx), "memory.reclaim")):
            return "kernel %s has no memory.reclaim (needs 5.19 or newer, e.g. RHEL 10)" % platform.release()
        return None

    def run(self, ctx, cfg):
        folder = user_slice(ctx)
        with open(os.path.join(folder, "memory.current")) as current:
            before = int(current.read())
        amount = int(before * float(cfg.get("reclaim_percent", 10)) / 100)
        if ctx.simulate:
            return "would ask the kernel to reclaim %s of the %s your apps use" % (fmt_bytes(amount), fmt_bytes(before))
        partial = False
        try:
            with open(os.path.join(folder, "memory.reclaim"), "w") as knob:
                knob.write("%d\n" % amount)
        except OSError as error:
            if error.errno != errno.EAGAIN:  # EAGAIN: the kernel reclaimed less than asked
                raise
            partial = True
        with open(os.path.join(folder, "memory.current")) as current:
            after = int(current.read())
        return "your apps went from %s to %s%s" % (fmt_bytes(before), fmt_bytes(after),
                                                  " (the kernel reclaimed less than asked)" if partial else "")


class DropCaches(Strategy):
    goal = "purge_memory_caches"
    label = "drop the page, dentry and inode caches"
    needs_admin = True

    def run(self, ctx, cfg):
        compact = cfg.get("compact_memory", True) and os.path.exists("/proc/sys/vm/compact_memory")
        if ctx.simulate:
            return "would sync disks, drop the page, dentry and inode caches" + (", compact memory" if compact else "")
        os.sync()
        with open("/proc/sys/vm/drop_caches", "w") as knob:
            knob.write("3\n")
        done = ["page, dentry and inode caches dropped"]
        if compact:
            with open("/proc/sys/vm/compact_memory", "w") as knob:
                knob.write("1\n")
            done.append("memory compacted")
        return ", ".join(done)


class ResolvectlFlush(Strategy):
    goal = "flush_dns"
    label = "resolvectl flush-caches (systemd-resolved)"

    def unavailable(self, ctx):
        if not shutil.which("resolvectl"):
            return "systemd-resolved isn't installed"
        if run(["systemctl", "is-active", "--quiet", "systemd-resolved"])[0] != 0:
            return "systemd-resolved isn't running, so there is no cache to flush"
        return None

    def run(self, ctx, cfg):
        return run_flush(ctx, ["resolvectl", "flush-caches"])


class NscdFlush(Strategy):
    goal = "flush_dns"
    label = "nscd -i hosts"

    def unavailable(self, ctx):
        if not shutil.which("nscd"):
            return "nscd isn't installed"
        if not any(norm(proc.info["name"]) == "nscd" for proc in psutil.process_iter(["name"])):
            return "nscd isn't running"
        return None

    def run(self, ctx, cfg):
        return run_flush(ctx, ["nscd", "-i", "hosts"])


def run_flush(ctx, command):
    if ctx.simulate:
        return "would run: " + " ".join(command)
    code, out = run(command)
    if code:
        raise RuntimeError("%s failed: %s" % (" ".join(command), out.splitlines()[-1] if out else code))
    return "DNS cache flushed"


STRATEGIES = [CgroupReclaim(), DropCaches(), ResolvectlFlush(), NscdFlush()]
