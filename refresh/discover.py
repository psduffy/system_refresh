"""--discover: find background apps worth stopping on every run, and approve them into the settings once."""

import json
import time

import psutil

from . import common
from .common import DEFAULT_CONFIG, OS_KEY, OS_NAME, fmt_bytes, for_this_os, matches, norm, strip_exe

GENERIC = "runs in the background"


def candidates(ctx):
    """Background apps this user could have stopped automatically, heaviest first:
    [{"name", "pids", "memory", "cpu", "labels", "exe", "approved"}]. Apps with a window, parts of the OS,
    work started from a terminal and processes the OS manages are left out."""
    classify = ctx.platform.discovery_classifier(ctx)
    procs = list(psutil.process_iter(["name", "exe", "ppid", "create_time", "cpu_times"]))
    names = {proc.pid: strip_exe(proc.info["name"] or "") for proc in procs}
    in_apps, app_names = app_families(ctx, procs, names)
    approved = set(norm(name) for name in for_this_os(ctx.config["steps"]["stop_background_apps"].get("names"), []))
    now = time.time()
    groups = {}
    for proc in procs:
        name = names[proc.pid]
        if not name or proc.pid in ctx.untouchable or ctx.is_protected(name) or not ctx.owns(proc):
            continue
        if proc.pid in in_apps or norm(name) in app_names:
            continue  # part of an app with a window: its helpers, web views and the tools it runs
        exe = proc.info["exe"] or ""
        reason, labels = classify(proc, name, exe, names.get(proc.info["ppid"], ""))
        if reason:
            continue
        group = groups.setdefault(norm(name), {"name": name, "pids": [], "memory": 0, "cpu": 0.0, "labels": [],
                                               "exe": exe, "approved": norm(name) in approved})
        group["pids"].append(proc.pid)
        group["memory"] += common.app_memory(proc)
        times, started = proc.info["cpu_times"], proc.info["create_time"]
        if times and started:
            group["cpu"] += 100.0 * (times.user + times.system) / max(1.0, now - started)
        for label in labels:
            if label not in group["labels"]:
                group["labels"].append(label)
    return sorted(groups.values(), key=lambda group: group["memory"], reverse=True)


def app_families(ctx, procs, names):
    """(pids, names) of the apps with a user interface and everything they started. The desktop shell
    (Explorer, Finder, ...) launches everything, so it doesn't count as an app here."""
    shells = ctx.platform.SHELLS
    roots = {pid for pid in ctx.platform.ui_pids(ctx, procs) if not matches(norm(names.get(pid, "")), shells)}
    children = {}
    for proc in procs:
        children.setdefault(proc.info["ppid"], []).append(proc)
    family, queue = set(), [proc for proc in procs if proc.pid in roots]
    while queue:
        node = queue.pop()
        if node.pid in family:
            continue
        family.add(node.pid)
        born = node.info["create_time"] or 0
        queue.extend(kid for kid in children.get(node.pid, []) if (kid.info["create_time"] or 0) >= born)
    return family, {norm(names[pid]) for pid in roots if names.get(pid)}


def show(ctx, found, limit=20):
    ctx.log("Background apps you could have stopped on every run (%s)" % OS_NAME, "head")
    if not found:
        ctx.log("none: everything running has a window, is part of %s, or is managed by it" % OS_NAME)
        return
    ctx.log("%3s  %-32s %10s %8s  %s" % ("#", "app", "memory", "avg CPU", "why it's listed"))
    for index, group in enumerate(found[:limit], 1):
        labels = [label for label in group["labels"] if label != GENERIC] or group["labels"]
        mark = "  (approved)" if group["approved"] else ""
        ctx.log("%3d  %-32s %10s %7.1f%%  %s%s" % (index, group["name"][:32], fmt_bytes(group["memory"]),
                                                  group["cpu"], ", ".join(labels), mark))
    if len(found) > limit:
        ctx.log("... and %d smaller ones" % (len(found) - limit))


def pick(found, answer):
    """Turns "1, 3, SomeApp" into app names; numbers refer to the list show() printed."""
    names = []
    for token in (answer or "").split(","):
        token = token.strip()
        if token.isdigit():
            index = int(token) - 1
            if 0 <= index < len(found):
                names.append(found[index]["name"])
        elif token:
            names.append(token)
    return names


def approve(config_path, names):
    """Adds names to this OS's stop_background_apps list in the settings file; returns the ones that were new."""
    with open(config_path, encoding="utf-8-sig") as source:
        raw = json.load(source)
    stop = raw.setdefault("steps", {}).setdefault("stop_background_apps", {})
    current = stop.get("names")
    if current is None:
        current = json.loads(json.dumps(DEFAULT_CONFIG["steps"]["stop_background_apps"]["names"]))
    bucket = current.setdefault(OS_KEY, []) if isinstance(current, dict) else current
    known = {norm(name) for name in bucket}
    added = []
    for name in names:
        if norm(name) not in known:
            bucket.append(name)
            known.add(norm(name))
            added.append(name)
    stop["names"] = current
    with open(config_path, "w", encoding="utf-8") as target:
        json.dump(raw, target, indent=2)
        target.write("\n")
    return added
