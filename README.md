# system_refresh

Prunes resource bloat on a running machine without a reboot. It ends hung and orphaned processes, stops
background apps you've approved, gets idle apps to give memory back, drops caches and clears stale temp files.
Every run logs a before/after snapshot and names the heaviest apps still running.

Runs on **Windows 10/11, macOS and Linux (RHEL 8+)**. Needs Python 3.6+ and one package, `psutil`.

It's organized around **goals**, not commands. Each OS offers different ways to reach a goal, so at startup
the tool checks what this machine has (OS version, admin rights, available tools, kernel features) and uses the
best way it finds. `--explain` shows what it found. `selftest.py` checks every goal against bloat it creates
itself (see [Self-test](#self-test)).

## Get it

```
git clone https://github.com/psduffy/system_refresh.git
cd system_refresh
```

Update later with `git pull`. Each machine keeps its own `refresh_config.json` and `logs/`. Neither is committed,
so a pull never overwrites them.

| OS | Install psutil |
|---|---|
| Windows | `py -m pip install psutil` |
| macOS | `python3 -m pip install --user psutil` |
| RHEL | `sudo dnf install python3-psutil` (or `python3 -m pip install --user psutil`) |

Keep the folder somewhere permanent: the settings file and `logs/` live next to the script, and `--install`
points the schedule at it. On Windows, use `py` instead of `python3` if `python3` opens the Microsoft Store.

## Use

```
python3 system_refresh.py --explain          # what this machine can do for each goal, and how
python3 system_refresh.py --discover         # pick background apps to stop on every run
python3 system_refresh.py --dry-run          # show what a run would do, change nothing
python3 system_refresh.py                    # standard refresh
python3 system_refresh.py --deep --elevate   # everything, as administrator (UAC) or root (sudo)
python3 system_refresh.py --only release_idle_memory,flush_dns
```

## Goals

They run in this order. A goal's mode decides when it runs: `on` every time, `deep` only with `--deep`, `off`
never. Some ways of reaching a goal are heavier than others; those are marked *deep only* and need `--deep` (or
`--only`) even when the goal itself is `on`.

| Goal | Mode | Windows | macOS | Linux |
|---|---|---|---|---|
| `kill_hung_apps` | deep | apps whose window is Not Responding | apps the window server flags Not Responding (undocumented call) | none: desktops show their own prompt |
| `stop_background_apps` | on | apps you approved | apps you approved | apps you approved |
| `prune_orphans` | deep | dev-tool trees whose parent died | same, launchd jobs excluded | same, systemd services excluded |
| `restart_shell` | deep | Explorer, past 600 MB | Finder, past 600 MB | none |
| `release_idle_memory` | on | trim background apps' working sets | low-memory warning to apps (`memory_pressure`), root, deep only | kernel reclaim (`memory.reclaim`), root, deep only, kernel 5.19+ |
| `purge_memory_caches` | deep | file cache, modified and standby lists, admin | `purge`, root | drop caches and compact, root |
| `clean_temp_files` | on | temp, crash dumps, error reports | `$TMPDIR`, diagnostic reports | `/tmp`, `/var/tmp` |
| `flush_dns` | on | `ipconfig /flushdns` | `dscacheutil` + mDNSResponder, root | `resolvectl` or `nscd`, if one runs |

Frozen apps are ended only if they are still frozen 10 seconds later. Orphans must be at least 2 hours old.
Whenever a goal can't run, the run says why (wrong OS, needs admin, missing tool, old kernel) and carries on.

## Choosing background apps: `--discover`

Nothing is stopped just because the tool found it. `--discover` lists the background apps you could have stopped
on every run, heaviest first, and says why each is listed: it starts at login, it's a launch agent, it's a menu
bar app. You pick the ones you want by number or name, and they're added to this OS's list in
`refresh_config.json`. From then on every run stops them automatically.

```
python3 system_refresh.py --discover                          # list, then ask which to approve
python3 system_refresh.py --discover --approve 2,OneDrive     # approve without being asked
```

It leaves out anything that's part of an app with a window (its helpers and web views, and tools it started),
anything that's part of the OS, anything started from a terminal, processes the OS manages (systemd services) and
protected processes. Windows ships with a starter list (Widgets, Game Bar, the Java and Adobe updaters); macOS and
Linux start empty.

## What it will never touch

- Itself or any process above it (the terminal, the scheduler).
- Built-in protected system processes per OS (svchost, dwm, csrss, installers, VM hosts, WindowServer, launchd,
  systemd, sshd, gnome-shell...). Add your own under `protected_processes`.
- Other users' processes. When run as root it acts for `$SUDO_USER`, or for `--target-user`.
- Orphans that are meant to be detached: anything with a visible window, launchd jobs, systemd services, or
  processes started with `nohup`.
- Temp files that are new, in use, or only recently unpacked (it checks creation time, not just modified time).
  It also skips anything behind symlinks or junctions, files on other drives, and on macOS/Linux files you don't own.
  It refuses outright to clean a drive root, a system folder or a home folder.

## Settings

`refresh_config.json` is written with the defaults on the first run. Edit it to change modes, thresholds and
lists; anything you leave out keeps its default. Any setting can be split by OS:

```json
"stop_background_apps": {
  "mode": "on",
  "names": {"windows": ["Widgets", "DellOptimizer.Systray"], "darwin": ["Microsoft AutoUpdate"], "linux": []}
}
```

Names match case-insensitively, without `.exe`, and accept wildcards (`python*`). Set `"dry_run": true` to make
every run, including scheduled ones, report without acting.

`release_idle_memory` has one setting group per OS: `min_working_set_mb` and `skip_foreground_app` (Windows),
`pressure_level` (`warn` or `critical`) and `pressure_seconds` (macOS), `reclaim_percent` (Linux).
`report` controls the closing summary: how many top apps to list, when to call an app heavy (1500 MB), and how
many handles counts as a likely leak (10,000, Windows only).

Settings files from before the goals redesign keep working: `trim_working_sets` is read as `release_idle_memory`.

## Run it automatically

```
python3 system_refresh.py --install --every 4                   # standard refresh every 4 hours
python3 system_refresh.py --install --every 2 --require-idle 10  # only when you've been away 10 minutes
python3 system_refresh.py --install --deep --elevate             # deep runs with admin/root rights
python3 system_refresh.py --uninstall
```

This uses Task Scheduler on Windows, launchd on macOS and cron on Linux. `--require-idle` works on Windows and macOS.
Install as administrator/root (`--elevate`) if scheduled runs should include the admin-only ways.

## Logs

Each refresh run (with or without `--dry-run`) writes `logs/refresh-YYYYMMDD-HHMMSS.log`, including per-process
detail the console leaves out. The newest 30 are kept (`keep_logs`). `selftest.py` saves its results as
`logs/selftest-YYYYMMDD-HHMMSS.log`. `--explain` and `--discover` only print to the screen.

## What to expect

The biggest wins come from ending things: hung apps, forgotten dev servers, leaky helpers. The closing report
tells you which heavy apps are left; restarting the one it names usually beats everything else.

Trimming and cache purges make "free" memory jump. But the OS was already treating that memory as reclaimable,
so apps can feel slightly slower for a moment while they page back in. That's why the purges are `deep`-only.

## Self-test

A freshly set-up machine has little to clean, so `selftest.py` creates its own bloat and checks each goal
against it. It starts a memory hog, a background app that ignores the polite stop signal, orphaned process trees
and a window that freezes on purpose. It also builds a sandbox of stale, fresh, read-only, in-use and linked
temp files, and checks that `--explain` and `--discover` work.

```
python3 selftest.py                          # every goal that can run here, about a minute
python3 selftest.py --only prune_orphans     # one test
python3 selftest.py --elevate --with-system  # also the system-wide memory goals, for real
```

It only acts on what it created. While a goal runs, the tool sees only the test processes, and any attempt to stop
or trim anything else is blocked and reported as a failure. Goals that act on the whole system run as dry runs:
`restart_shell` always, and `purge_memory_caches` (plus `release_idle_memory` on macOS and Linux) unless you add
`--with-system`. On macOS the frozen-window test may ask you to click its window: macOS only flags an app as Not
Responding once it misses events. Test processes end themselves after 5 minutes even if the test is interrupted.

It prints PASS, FAIL or SKIP per test and exits with code 1 if anything failed. A goal this machine can't reach
shows as SKIP with the reason.

## Tested

**Windows 11** (Python 3.14, psutil 7.2): `selftest.py` passes every test across a standard-user run and an
elevated `--with-system` run, which included a real purge of the standby cache. `--install` and `--uninstall` were
checked with a dry-run scheduled task.

**macOS 15.8** (MacBook Pro, Python 3.13, `--elevate --with-system`): `selftest.py` passes all 11 tests across
three runs, including the Not Responding check, a real memory-pressure warning, a real purge, a real DNS flush and
discovery. Getting there took two fixes:
- An orphan started with `nohup` wasn't recognized, so it was ended. macOS's `ps` has no field for ignored signals,
  so the tool now reads them from the kernel's process record (`sysctl` `KERN_PROC_PID`). If it still can't tell, it
  keeps the orphan.
- The self-test's Finder dry run looked for root's Finder instead of yours.

**Linux:** `selftest.py` has run only in an Ubuntu 24.04 container (as root, cgroup v1, no desktop): 7 passed and
4 skipped because the container lacks what those goals use. It has not run on RHEL or a Linux desktop.

**Not yet run:** RHEL, scheduling through launchd and cron, and a real Explorer or Finder restart.
