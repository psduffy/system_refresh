"""The before/after snapshot of memory and load, and the report on the heaviest apps still running."""

import psutil

from .common import IS_WINDOWS, MB, app_memory, fmt_bytes, fmt_metric, norm, strip_exe


def snapshot(ctx):
    """[(label, kind, value)] describing memory and process load right now."""
    memory = psutil.virtual_memory()
    rows = [("Available RAM", "bytes", memory.available)]
    rows += ctx.platform.memory_rows(memory)
    rows.append(("Swap used", "bytes", psutil.swap_memory().used))
    load = ctx.platform.load_rows()
    if load is None:
        processes = threads = 0
        for proc in psutil.process_iter(["num_threads"]):
            processes += 1
            threads += proc.info["num_threads"] or 0
        load = [("Processes", "count", processes), ("Threads", "count", threads)]
    return rows + load


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
