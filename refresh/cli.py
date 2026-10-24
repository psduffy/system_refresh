"""The command line: a refresh run, --explain, --discover, or scheduling."""

import argparse
import os
import sys
import time
from datetime import datetime

from . import common, discover, goals, report, schedule
from .common import ADMIN, IS_WINDOWS, RENAMED_STEPS, STEP_ORDER, StepSkipped


def step_list(text):
    return [RENAMED_STEPS.get(step.strip(), step.strip()) for step in text.split(",") if step.strip()]


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Prune resource bloat on a running machine; the next best thing to a reboot.",
        epilog="Goals, in run order: " + ", ".join(STEP_ORDER))
    parser.add_argument("--deep", action="store_true",
                        help='also work on "deep" goals, and use the heavier ways of reaching the others')
    parser.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    parser.add_argument("--only", type=step_list, default=[], metavar="GOALS",
                        help="work on just these comma-separated goals, whatever their mode")
    parser.add_argument("--skip", type=step_list, default=[], metavar="GOALS", help="never work on these goals")
    parser.add_argument("--config", default=common.DEFAULT_CONFIG_PATH, help="settings file (default: %(default)s)")
    parser.add_argument("--elevate", action="store_true",
                        help="relaunch as administrator (UAC prompt) or root (sudo) for the admin-only ways")
    parser.add_argument("--require-idle", type=float, default=0, metavar="MINUTES",
                        help="do nothing unless there has been no keyboard or mouse input for this long "
                             "(Windows and macOS)")
    parser.add_argument("--target-user", metavar="USER",
                        help="when running as root: whose processes and temp files to clean (default: $SUDO_USER)")
    parser.add_argument("--quiet", action="store_true", help="write to the log file only")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    found = parser.add_argument_group("finding out")
    found.add_argument("--explain", action="store_true",
                       help="show, for every goal, the ways this machine can reach it and which one is used")
    found.add_argument("--discover", action="store_true",
                       help="list background apps worth stopping on every run, and approve the ones you pick")
    found.add_argument("--approve", metavar="APPS",
                       help="with --discover: approve these comma-separated names or list numbers without asking")
    schedule_group = parser.add_argument_group("scheduling")
    schedule_group.add_argument("--install", action="store_true",
                                help="run automatically from Task Scheduler, launchd or cron; combine with "
                                     "--deep, --require-idle and --config to choose how")
    schedule_group.add_argument("--every", type=float, default=4, metavar="HOURS",
                                help="interval for --install (default: 4)")
    schedule_group.add_argument("--uninstall", action="store_true", help="remove the scheduled run")
    args = parser.parse_args(argv)
    unknown = [step for step in args.only + args.skip if step not in STEP_ORDER]
    if unknown:
        parser.error("unknown goal(s): %s" % ", ".join(unknown))
    return args


def pause_if_asked(args):
    if args.pause:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass


def run_discover(ctx, args, config_path):
    found = discover.candidates(ctx)
    discover.show(ctx, found)
    if args.approve:
        answer = args.approve
    elif found and common.stdin_is_terminal():
        try:
            answer = input("\nStop which of these on every run? Numbers or names, comma-separated (Enter for none): ")
        except EOFError:
            answer = ""
    else:
        if found:
            ctx.log("to approve some, run --discover in a terminal, or add --approve NAME,NAME", "skip")
        return 0
    names = []
    for name in discover.pick(found, answer):
        if ctx.is_protected(name):
            ctx.log("%s is a protected process; not added" % name, "warn")
        else:
            names.append(name)
    if not names:
        ctx.log("nothing added", "skip")
        return 0
    added = discover.approve(config_path, names)
    ctx.give_back(config_path)
    if added:
        ctx.log("approved for %s: %s. Every run will stop them from now on." % (common.OS_NAME, ", ".join(added)), "ok")
    else:
        ctx.log("already approved: %s" % ", ".join(names), "skip")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # window titles may not fit the console's code page
    if args.elevate and not common.is_admin():
        return schedule.relaunch_elevated(argv)
    if args.uninstall:
        code = schedule.uninstall()
        pause_if_asked(args)
        return code
    config_path = os.path.abspath(args.config)
    try:
        config, created = common.load_config(config_path)
    except (OSError, ValueError) as error:
        print("Could not read %s: %s" % (config_path, error))
        return 2
    if args.install:
        code = schedule.install(args, config_path)
        pause_if_asked(args)
        return code

    log = common.Log(args.quiet)
    if args.explain or args.discover or args.approve:
        ctx = make_context(args, config, log)
        if ctx is None:
            return 2
        if created:
            ctx.give_back(config_path)
        code = 0
        if args.explain:
            goals.explain(ctx)
        else:
            code = run_discover(ctx, args, config_path)
        pause_if_asked(args)
        return code

    log_dir = os.path.join(common.SCRIPT_DIR, os.path.expandvars(os.path.expanduser(config.get("log_directory", "logs"))))
    os.makedirs(log_dir, exist_ok=True)
    log.open(os.path.join(log_dir, "refresh-%s.log" % datetime.now().strftime("%Y%m%d-%H%M%S")))
    old_logs = sorted(name for name in os.listdir(log_dir) if name.startswith("refresh-") and name.endswith(".log"))
    for name in old_logs[:-max(1, int(config.get("keep_logs", 30)))]:
        try:
            os.remove(os.path.join(log_dir, name))
        except OSError:
            pass

    ctx = make_context(args, config, log)
    if ctx is None:
        return 2
    for path in (log_dir, log.path) + ((config_path,) if created else ()):
        ctx.give_back(path)
    started = time.time()
    mode = "only " + ", ".join(args.only) if args.only else "deep" if args.deep else "standard"
    who = ADMIN if ctx.is_admin else "standard user"
    if not IS_WINDOWS and ctx.is_admin and ctx.user != "root":
        who += ", acting for " + ctx.user
    log("System refresh on %s: %s%s, %s" % (common.os_label(), mode,
                                            " (dry run, nothing will change)" if ctx.simulate else "", who), "head")
    if created:
        log("wrote default settings to %s" % config_path)
    for name in config.get("steps", {}):
        if name not in STEP_ORDER:
            log("ignoring unknown goal '%s' in %s" % (name, config_path), "warn")

    if args.require_idle:
        idle = ctx.platform.idle_seconds()
        if idle is None:
            log("can't measure idle time on %s; running anyway" % common.OS_NAME, "warn")
        elif idle < args.require_idle * 60:
            log("last input %.0f s ago; waiting for %g idle minutes, so nothing was done" % (idle, args.require_idle),
                "skip")
            return 0

    before = report.snapshot(ctx)
    results, deep_only = [], []
    for name in STEP_ORDER:
        step_mode = str(config["steps"].get(name, {}).get("mode", "off")).lower()
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
            outcome, level = goals.run_goal(ctx, name), "ok"
        except StepSkipped as reason:
            outcome, level = "skipped: %s" % reason, "skip"
        except Exception as error:  # one failing goal must not stop the rest
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
        report.log_snapshot(log, before)
    else:
        time.sleep(2)  # let the counters settle
        report.log_snapshot(log, before, report.snapshot(ctx))
    report.report_heavy_hitters(ctx)
    log("finished in %.1f s; log: %s" % (time.time() - started, log.path), "head")
    pause_if_asked(args)
    return 0


def make_context(args, config, log):
    try:
        return common.Context(args, config, log)
    except KeyError:
        print("No such user: %s" % (args.target_user or os.environ.get("SUDO_USER")))
        return None
