"""The goals system_refresh works toward, the ways that work on every OS, and how it picks one per machine."""

import os
import stat
import time

import psutil

from . import common
from .common import (ADMIN, IS_WINDOWS, OS_NAME, STEP_ORDER, StepSkipped, Strategy,
                     fmt_age, fmt_bytes, for_this_os, matches, norm, strip_exe)


class ApprovedApps(Strategy):
    goal = "stop_background_apps"
    label = "stop the background apps you approved"

    def unavailable(self, ctx):
        if not approved_names(ctx):
            return "no apps approved for %s yet; run --discover to pick some" % OS_NAME
        return None

    def run(self, ctx, cfg):
        patterns = approved_names(ctx)
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
            return "none of the %d approved apps were running" % len(patterns)
        count, freed = common.stop_processes(ctx, targets, "approved background app")
        return "%s %d process(es), %s" % ("would stop" if ctx.simulate else "stopped", count, fmt_bytes(freed))


def approved_names(ctx):
    return [norm(name) for name in for_this_os(ctx.config["steps"]["stop_background_apps"].get("names"), [])]


class OrphanTrees(Strategy):
    goal = "prune_orphans"
    label = "end orphaned dev-tool process trees"

    def run(self, ctx, cfg):
        plat = ctx.platform
        patterns = [norm(name) for name in for_this_os(cfg.get("names"), [])]
        cutoff = time.time() - float(cfg.get("min_age_minutes", 120)) * 60
        procs, children = {}, {}
        for proc in psutil.process_iter(["ppid", "name", "create_time"]):
            procs[proc.pid] = proc
            children.setdefault(proc.info["ppid"], []).append(proc)
        keeper = plat.orphan_keeper(ctx)
        handled = set()
        trees = count = freed = 0
        for proc in list(procs.values()):
            name = strip_exe(proc.info["name"] or "")
            started = proc.info["create_time"]
            if proc.pid in handled or not started or started > cutoff:
                continue
            if not matches(norm(name), patterns) or not plat.is_orphan(proc, procs) or not ctx.owns(proc):
                continue
            tree = collect_tree(proc, children, handled)
            label = "%s [%d]: parent gone, running %.1f h, %d process(es)" % (
                name, proc.pid, (time.time() - started) / 3600, len(tree))
            reason = self.keep_reason(ctx, tree, keeper)
            if reason:
                ctx.log("%s; kept because %s" % (label, reason), "skip")
                continue
            ctx.log(label)
            trees += 1
            stopped, memory = common.stop_processes(ctx, reversed(tree), "orphaned %s tree" % name)
            count += stopped
            freed += memory
        if not trees:
            return "no orphaned process trees"
        verb = "would end" if ctx.simulate else "ended"
        return "%s %d orphaned tree(s): %d process(es), %s" % (verb, trees, count, fmt_bytes(freed))

    @staticmethod
    def keep_reason(ctx, tree, keeper):
        """Why an orphaned tree must be left alone, or None if it can go."""
        for node in tree:
            name = strip_exe(node.info["name"] or "")
            if node.pid in ctx.untouchable:
                return "it contains this script"
            if ctx.is_protected(name):
                return "%s is protected" % name
            reason = keeper(node, name)
            if reason:
                return reason
        nohup = ctx.platform.started_with_nohup(tree[0].pid)
        if nohup is None:  # not knowing must never end something its owner meant to keep running
            return "couldn't check whether it was started with nohup"
        if nohup:
            return "it was started with nohup"
        return None


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


class StaleTempFiles(Strategy):
    goal = "clean_temp_files"
    label = "delete stale temp files, crash dumps and error reports"

    def run(self, ctx, cfg):
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


SHARED = [ApprovedApps(), OrphanTrees(), StaleTempFiles()]


# ---------------------------------------------------------------- choosing a way per machine

def assess(ctx, goal):
    """Every way this machine could reach the goal, best first: [(strategy, why it can't run here or None)].
    A goal with no way at all on this OS comes back as [(None, reason)]."""
    options = [strategy for strategy in SHARED + ctx.platform.STRATEGIES if strategy.goal == goal]
    if not options:
        return [(None, ctx.platform.UNSUPPORTED.get(goal, "nothing on %s can do this" % OS_NAME))]
    rows = []
    for strategy in options:
        reason = strategy.unavailable(ctx)
        if reason is None and strategy.needs_admin and not ctx.is_admin:
            reason = "needs %s (add --elevate)" % ADMIN
        rows.append((strategy, reason))
    return rows


def choose(ctx, goal):
    rows = assess(ctx, goal)
    usable = [strategy for strategy, reason in rows if strategy and reason is None]
    for strategy in usable:
        if ctx.deep or not strategy.deep_only:
            return strategy
    if usable:
        raise StepSkipped("%s only runs with --deep" % usable[0].label)
    raise StepSkipped(rows[0][1])


def run_goal(ctx, goal):
    """Works toward one goal the best way this machine allows; returns what happened."""
    strategy = choose(ctx, goal)
    ctx.log("via: " + strategy.label)
    return strategy.run(ctx, ctx.config["steps"].get(goal, {}))


def explain(ctx):
    """--explain: for every goal, each way this OS has of reaching it and whether it can run here."""
    who = ADMIN if ctx.is_admin else "standard user"
    if not IS_WINDOWS and ctx.is_admin and ctx.user != "root":
        who += ", acting for " + ctx.user
    ctx.log("What system_refresh can do on %s (%s)" % (common.os_label(), who), "head")
    for goal in STEP_ORDER:
        mode = str(ctx.config["steps"].get(goal, {}).get("mode", "off")).lower()
        ctx.log("%s (mode: %s)" % (goal, mode), "head")
        chosen = None
        for strategy, reason in assess(ctx, goal):
            if strategy is None:
                ctx.log("not on %s: %s" % (OS_NAME, reason), "skip")
                continue
            note = " [deep only]" if strategy.deep_only else ""
            if reason:
                ctx.log("%s%s: %s" % (strategy.label, note, reason), "skip")
            elif chosen is None:
                chosen = strategy
                ctx.log("%s%s: ready, used" % (strategy.label, note), "ok")
            else:
                ctx.log("%s%s: ready, fallback" % (strategy.label, note))
