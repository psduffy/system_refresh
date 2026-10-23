#!/usr/bin/env python3
"""
selftest: checks every system_refresh step against bloat it creates on purpose.

It starts its own throwaway processes (a memory hog, a background app that ignores the polite
stop signal, orphaned process trees, and on Windows a window that freezes) plus a sandbox of temp
files. Then it points each step at them and checks the result. It never acts on anything it didn't
create: while a step runs, the tool sees only the test processes, and any attempt to stop or trim
anything else is blocked and counted as a failure.

  python3 selftest.py                          test every step that can run here
  python3 selftest.py --only prune_orphans     test one step
  python3 selftest.py --elevate --with-system  also purge memory caches for real (needs admin/root)

restart_shell always runs as a dry run, because a real run would restart your actual Explorer or
Finder. purge_memory_caches is a dry run too unless you add --with-system.
"""

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback

SELF = os.path.abspath(__file__)
sys.path.insert(0, os.path.dirname(SELF))
import system_refresh as sr  # noqa: E402  (explains how to install psutil if it's missing)
import psutil  # noqa: E402

NO_WINDOW = 0x08000000 if sr.IS_WINDOWS else 0  # CREATE_NO_WINDOW
LIFETIME = 300  # seconds; test processes end on their own even if cleanup never runs
FILE_AGE = 8    # seconds; the temp-file test treats anything older than this as stale

SPAWNED = []  # psutil.Process for everything the test started, ended during cleanup
POPENS = []   # our direct children, reaped during cleanup


class Failed(Exception):
    """A test whose check did not hold; the message says what went wrong."""


# ---------------------------------------------------------------- test processes

def start(role, *args, **popen_args):
    """Starts this script in a helper role as a background process."""
    popen = subprocess.Popen([sys.executable, SELF, "--role", role] + list(args),
                             stdin=subprocess.DEVNULL, stdout=popen_args.get("stdout", subprocess.DEVNULL),
                             stderr=subprocess.DEVNULL, universal_newlines=True, creationflags=NO_WINDOW)
    POPENS.append(popen)
    return popen


def track(popen_or_pid):
    proc = psutil.Process(getattr(popen_or_pid, "pid", popen_or_pid))
    SPAWNED.append(proc)
    return proc


def orphan(*role):
    """Starts a role through a launcher that exits at once, so the role is left without a parent."""
    launcher = start("launch", *role, stdout=subprocess.PIPE)
    out, _ = launcher.communicate(timeout=30)
    return track(int(out.strip()))


def family(proc):
    try:
        return [proc] + proc.children(recursive=True)
    except psutil.Error:
        return [proc]


def alive(proc):
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def role_sleep(args):
    if "stubborn" in args and not sr.IS_WINDOWS:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # makes the tool escalate to a kill
    time.sleep(LIFETIME)


def role_hog(args):
    ballast = b"x" * (300 * sr.MB)  # written out, so every page is really in RAM
    time.sleep(LIFETIME)
    return len(ballast)


def role_launch(args):
    print(start(*args).pid)


def role_middle(args):
    if "nohup" in args:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    start("sleep")
    time.sleep(LIFETIME)


def role_window(args):
    """A visible window that keeps answering ("pump") or stops after a second ("hang")."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32")
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    message_pointer = ctypes.POINTER(wintypes.MSG)
    user32.PeekMessageW.argtypes = [message_pointer, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [message_pointer]
    user32.DispatchMessageW.argtypes = [message_pointer]
    hang = args[0] == "hang"
    title = "system_refresh self-test: " + ("this window freezes on purpose" if hang else "test window")
    user32.CreateWindowExW(0, "STATIC", title, 0x10CF0000,  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
                           120, 120, 560, 120, None, None, None, None)
    message = wintypes.MSG()
    end = time.time() + (1 if hang else LIFETIME)
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):  # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        time.sleep(0.05)
    if hang:
        time.sleep(LIFETIME)  # no more message handling: Windows will call the window "Not Responding"


ROLES = {"sleep": role_sleep, "hog": role_hog, "launch": role_launch, "middle": role_middle, "window": role_window}


# ---------------------------------------------------------------- running steps under guard

class Guard(object):
    """While a step runs, limits the tool to the test processes and blocks actions on anything else."""

    def __init__(self, procs, isolate=True):
        self.isolate = isolate
        self.allowed, self.visible, self.blocked = set(), set(), set()
        for proc in procs:
            for member in family(proc):
                self.allowed.add(member.pid)
                self.visible.add(member.pid)
                try:
                    self.visible.add(member.ppid())  # orphan checks need to see the parent
                except psutil.Error:
                    pass

    def __enter__(self):
        saved = self.saved = {"process_iter": psutil.process_iter}
        for name in ("stop_processes", "win_trim", "win_hung_windows"):
            if hasattr(sr, name):
                saved[name] = getattr(sr, name)

        if self.isolate:
            def process_iter(*args, **kwargs):
                return (proc for proc in saved["process_iter"](*args, **kwargs) if proc.pid in self.visible)
            psutil.process_iter = process_iter

        def stop_processes(ctx, procs, reason):
            procs = list(procs)
            self.blocked.update(proc.pid for proc in procs if proc.pid not in self.allowed)
            return saved["stop_processes"](ctx, [proc for proc in procs if proc.pid in self.allowed], reason)
        sr.stop_processes = stop_processes

        if "win_trim" in saved:
            def win_trim(pid, check_only=False):
                if pid not in self.allowed:
                    self.blocked.add(pid)
                    return False
                return saved["win_trim"](pid, check_only)
            sr.win_trim = win_trim

        if "win_hung_windows" in saved:
            def win_hung_windows():
                return {pid: title for pid, title in saved["win_hung_windows"]().items() if pid in self.allowed}
            sr.win_hung_windows = win_hung_windows
        return self

    def __exit__(self, *exc_info):
        psutil.process_iter = self.saved.pop("process_iter")
        for name, function in self.saved.items():
            setattr(sr, name, function)
        return False


class RecordingLog(sr.Log):
    """The tool's console log, keeping each message so a test can check what the step reported."""

    def __init__(self):
        sr.Log.__init__(self, quiet=False)
        self.records = []

    def __call__(self, message, level="info"):
        self.records.append((level, message))
        sr.Log.__call__(self, message, level)

    def saw(self, text, level=None):
        return any(text in message and level in (None, seen) for seen, message in self.records)


class Env(object):
    def __init__(self, args, sandbox, log):
        self.args = args
        self.sandbox = sandbox
        self.log = log

    def context(self, simulate=False):
        user = None if sr.IS_WINDOWS else current_user()
        options = argparse.Namespace(dry_run=simulate, target_user=user)
        return sr.Context(options, json.loads(json.dumps(sr.DEFAULT_CONFIG)), self.log)


def current_user():
    """The account running the test, which owns everything the test creates (root under sudo)."""
    if sr.IS_WINDOWS:
        import getpass
        return getpass.getuser()
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def run_step(env, name, settings, procs=(), simulate=False, isolate=True):
    """Runs one tool step with the given settings and returns its outcome."""
    cfg = json.loads(json.dumps(sr.DEFAULT_CONFIG["steps"][name]))
    cfg.update(settings)
    with Guard(procs, isolate) as guard:
        outcome = sr.STEPS[name](env.context(simulate), cfg)
    env.log(outcome, "ok")
    if guard.blocked:
        raise Failed("it tried to act on processes the test didn't create (PIDs %s); they were left alone"
                     % ", ".join(str(pid) for pid in sorted(guard.blocked)))
    return outcome


# ---------------------------------------------------------------- tests

def test_full_dry_run(env):
    """the whole tool end to end: a deep dry run of every step"""
    config = os.path.join(env.sandbox, "dry_run_config.json")
    with open(config, "w") as target:
        json.dump({"log_directory": os.path.join(env.sandbox, "logs")}, target)
    result = subprocess.run([sys.executable, sr.SCRIPT_PATH, "--dry-run", "--deep", "--config", config],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                            errors="replace", timeout=300, creationflags=NO_WINDOW)
    output = result.stdout
    lines = output.splitlines()
    showing = False
    for line in lines:
        showing = showing or line.startswith("== Summary")
        if showing:
            print("    | " + line)
    if result.returncode or "Traceback" in output:
        tail = lines[-1].strip() if lines else ""
        raise Failed("the tool exited with code %d: %s" % (result.returncode, tail))
    missing = [step for step in sr.STEP_ORDER if step not in output]
    if missing:
        raise Failed("no result from " + ", ".join(missing))
    failures = [line.strip() for line in lines if "failed:" in line]
    if failures:
        raise Failed(failures[0])
    if "Available RAM" not in output or "Heaviest apps" not in output:
        raise Failed("the memory snapshot or the app report is missing")
    return "all %d steps reported; the memory snapshot and app report printed" % len(sr.STEP_ORDER)


def test_kill_hung_apps(env):
    """a window that freezes on purpose"""
    if not sr.IS_WINDOWS:
        raise sr.StepSkipped("Windows only")
    frozen = track(start("window", "hang"))
    deadline = time.time() + 20
    while time.time() < deadline and frozen.pid not in sr.win_hung_windows():
        time.sleep(1)
    if frozen.pid not in sr.win_hung_windows():
        raise Failed("the test window never showed as Not Responding")
    run_step(env, "kill_hung_apps", {"confirm_seconds": 3}, [frozen])
    if alive(frozen):
        raise Failed("the frozen test window is still running")
    return "found the frozen test window, re-checked it, and ended it"


def test_stop_background_apps(env):
    """a listed background app that ignores the polite stop signal"""
    app = track(start("sleep", "stubborn"))
    time.sleep(1)  # let the process name settle (macOS framework Pythons re-exec at startup)
    name = sr.norm(app.name())
    run_step(env, "stop_background_apps", {"names": {key: [name] for key in sr.OS_KEYS}}, [app])
    if alive(app):
        raise Failed("the test app is still running")
    if sr.IS_WINDOWS:
        return "stopped the listed test app"
    return "stopped the listed test app; it ignored SIGTERM, so the follow-up kill did it"


def test_prune_orphans(env):
    """an orphaned process tree, and an orphan meant to outlive its parent"""
    doomed = orphan("middle")
    keeper = orphan("window", "pump") if sr.IS_WINDOWS else orphan("middle", "nohup")
    time.sleep(2)  # let the trees and the window come up
    doomed_tree = family(doomed)
    SPAWNED.extend(doomed_tree + family(keeper))
    run_step(env, "prune_orphans", {"min_age_minutes": 0, "names": [sr.norm(doomed.name())]}, [doomed, keeper])
    survivors = [proc for proc in doomed_tree if alive(proc)]
    if survivors:
        raise Failed("%d of the %d orphaned test processes are still running" % (len(survivors), len(doomed_tree)))
    if not alive(keeper):
        raise Failed("it also ended the orphan that should have been kept")
    reason = "has a window open" if sr.IS_WINDOWS else "was started with nohup"
    return "ended the orphaned test tree (%d processes) and kept the orphan that %s" % (len(doomed_tree), reason)


def test_restart_shell(env):
    """dry run only: a real run would restart your actual Explorer or Finder"""
    return "dry run: " + run_step(env, "restart_shell", {}, simulate=True, isolate=False)


def test_trim_working_sets(env):
    """a background app holding 300 MB"""
    if not sr.IS_WINDOWS:
        raise sr.StepSkipped("Windows only; %s has no way to trim another app's memory" % sr.OS_NAME)
    hog = track(start("hog"))
    deadline = time.time() + 30
    while time.time() < deadline and hog.memory_info().rss < 250 * sr.MB:
        time.sleep(0.5)
    before = hog.memory_info().rss
    run_step(env, "trim_working_sets", {"min_working_set_mb": 100, "skip_foreground_app": False}, [hog])
    after = hog.memory_info().rss
    if after > before / 2:
        raise Failed("the test app's working set only went from %s to %s" % (sr.fmt_bytes(before), sr.fmt_bytes(after)))
    return "the test app's working set went from %s to %s" % (sr.fmt_bytes(before), sr.fmt_bytes(after))


def cache_bytes():
    if sr.IS_WINDOWS:
        return sr.win_memory_lists().get("standby", 0)
    memory = psutil.virtual_memory()
    if sr.IS_MAC:
        return memory.inactive
    return getattr(memory, "cached", 0) + getattr(memory, "buffers", 0)


def test_purge_memory_caches(env):
    """the system-wide memory purge (a dry run unless --with-system)"""
    if not env.args.with_system:
        return "dry run: %s (add --with-system to purge for real)" % run_step(
            env, "purge_memory_caches", {}, simulate=True)
    before = cache_bytes()
    outcome = run_step(env, "purge_memory_caches", {})
    warnings = [message for level, message in env.log.records if level == "warn"]
    if warnings:
        raise Failed(warnings[0])
    return "%s; cache %s -> %s" % (outcome, sr.fmt_bytes(before), sr.fmt_bytes(cache_bytes()))


def make_link(target, link):
    if not sr.IS_WINDOWS:
        os.symlink(target, link)
        return
    try:
        import _winapi
        _winapi.CreateJunction(target, link)
    except (ImportError, AttributeError, OSError):
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], stdout=subprocess.DEVNULL, check=True)


def test_clean_temp_files(env):
    """a sandbox of stale, fresh, read-only, in-use and linked files"""
    root = os.path.join(env.sandbox, "temp")
    outside = os.path.join(env.sandbox, "outside")
    os.makedirs(os.path.join(root, "old_folder"))
    os.makedirs(outside)

    def write(*parts):
        path = os.path.join(*parts)
        with open(path, "w") as target:
            target.write("x" * 1000)
        return path

    stale = [write(root, "stale.txt"), write(root, "read_only.txt"), write(root, "old_folder", "stale.txt")]
    os.chmod(stale[1], stat.S_IREAD)
    in_use = write(root, "in_use.txt") if sr.IS_WINDOWS else None
    precious = write(outside, "precious.txt")
    make_link(outside, os.path.join(root, "link"))
    time.sleep(FILE_AGE + 2)
    fresh = write(root, "fresh.txt")
    unpacked = write(root, "just_unpacked.txt")  # modified "yesterday", like a file an installer just unpacked
    yesterday = time.time() - 86400
    os.utime(unpacked, (yesterday, yesterday))

    ctx_home = env.context().home
    settings = {"older_than_days": FILE_AGE / 86400.0, "paths": [root, ctx_home]}
    holder = open(in_use, "rb") if in_use else None
    try:
        run_step(env, "clean_temp_files", settings)
    finally:
        if holder:
            holder.close()

    problems = ["%s survived" % os.path.relpath(path, env.sandbox) for path in stale if os.path.exists(path)]
    if os.path.exists(os.path.join(root, "old_folder")):
        problems.append("the emptied old folder survived")
    for path in [fresh, unpacked, precious] + ([in_use] if in_use else []):
        if not os.path.exists(path):
            problems.append("%s was deleted" % os.path.relpath(path, env.sandbox))
    if not os.path.lexists(os.path.join(root, "link")):
        problems.append("the link was deleted")
    if not env.log.saw("refusing to clean", "warn"):
        problems.append("it didn't refuse the home folder")
    if problems:
        raise Failed("; ".join(problems))
    kept = "fresh, in-use and just-unpacked files" if in_use else "fresh and just-unpacked files"
    return "deleted the 3 stale files and the emptied folder; kept the %s; didn't follow the link; " \
           "refused the home folder" % kept


def test_flush_dns(env):
    """a real DNS cache flush (harmless: names are simply looked up again)"""
    return run_step(env, "flush_dns", {}, isolate=False)


TESTS = [("full_dry_run", test_full_dry_run)] + [(step, globals()["test_" + step]) for step in sr.STEP_ORDER]


# ---------------------------------------------------------------- main

def cleanup():
    for proc in SPAWNED:
        for member in reversed(family(proc)):
            try:
                member.kill()
            except psutil.Error:
                pass
    for popen in POPENS:
        try:
            popen.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def remove_sandbox(path):
    link = os.path.join(path, "temp", "link")
    if os.path.lexists(link):  # unlink first, so nothing behind the link is touched
        try:
            os.rmdir(link) if sr.IS_WINDOWS else os.unlink(link)
        except OSError:
            pass
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(folder, name), stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def parse_args(argv):
    names = [name for name, _ in TESTS]
    parser = argparse.ArgumentParser(description="Check every system_refresh step against bloat this test creates.")
    parser.add_argument("--only", type=sr.step_list, default=[], metavar="TESTS",
                        help="run just these comma-separated tests: " + ", ".join(names))
    parser.add_argument("--with-system", action="store_true",
                        help="purge memory caches for real (needs administrator or root)")
    parser.add_argument("--elevate", action="store_true", help="relaunch as administrator (UAC) or root (sudo)")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    unknown = [name for name in args.only if name not in names]
    if unknown:
        parser.error("unknown test(s): " + ", ".join(unknown))
    return args


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--role"]:
        ROLES[argv[1]](argv[2:])
        return 0
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.elevate and not sr.is_admin():
        return sr.relaunch_elevated(argv, script=SELF)

    log = RecordingLog()
    who = ("administrator" if sr.IS_WINDOWS else "root") if sr.is_admin() else "standard user"
    log("system_refresh self-test on %s: Python %s, psutil %s, %s" % (
        sr.OS_NAME, sys.version.split()[0], psutil.__version__, who), "head")
    sandbox = tempfile.mkdtemp(prefix="system_refresh_selftest_")
    env = Env(args, sandbox, log)
    results = []
    try:
        for name, test in TESTS:
            if args.only and name not in args.only:
                continue
            log("%s: %s" % (name, test.__doc__.strip()), "head")
            del log.records[:]
            try:
                status, detail = "PASS", test(env)
            except sr.StepSkipped as reason:
                status, detail = "SKIP", str(reason)
            except Failed as reason:
                status, detail = "FAIL", str(reason)
            except Exception as error:  # a crash in one test must not stop the others
                traceback.print_exc()
                status, detail = "FAIL", "error: %s" % error
            log("%s: %s" % (status, detail), {"PASS": "ok", "FAIL": "warn", "SKIP": "skip"}[status])
            results.append((name, status, detail))
    finally:
        cleanup()
        remove_sandbox(sandbox)

    log("Results", "head")
    for name, status, detail in results:
        log("%s  %-21s %s" % (status, name, detail), {"PASS": "ok", "FAIL": "warn", "SKIP": "skip"}[status])
    counts = dict((status, sum(1 for _, seen, _ in results if seen == status)) for status in ("PASS", "FAIL", "SKIP"))
    log("%d passed, %d failed, %d skipped" % (counts["PASS"], counts["FAIL"], counts["SKIP"]), "head")
    if args.pause:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
