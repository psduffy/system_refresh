# system_refresh

Prunes resource bloat on a running machine without a reboot. It ends hung and orphaned
processes, stops background helpers you name, squeezes idle memory out of background apps,
drops caches and clears stale temp files. Every run logs a before/after snapshot and names
the heaviest apps still running.

Runs on **Windows 10/11, macOS and Linux (RHEL 8+)**. One file, Python 3.6+, one dependency: `psutil`.
`selftest.py` checks every step against bloat it creates itself (see [Self-test](#self-test)).

## Get it

```
git clone https://github.com/psduffy/system_refresh.git
cd system_refresh
```

Update later with `git pull`. Each machine keeps its own `refresh_config.json` and `logs/`. Neither is committed,
so a pull never overwrites them.

## Install

| OS | Command |
|---|---|
| Windows | `py -m pip install psutil` |
| macOS | `python3 -m pip install --user psutil` |
| RHEL | `sudo dnf install python3-psutil` (or `python3 -m pip install --user psutil`) |

Keep the folder somewhere permanent: the settings file and `logs/` live next to the script, and `--install`
points the schedule at it.

## Use

```
python3 system_refresh.py --dry-run          # show what would happen, change nothing
python3 system_refresh.py                    # standard refresh
python3 system_refresh.py --deep             # also the more forceful steps
python3 system_refresh.py --deep --elevate   # everything, as administrator (UAC) or root (sudo)
python3 system_refresh.py --only trim_working_sets,flush_dns
python3 system_refresh.py --skip clean_temp_files
```

On Windows, use `py` instead of `python3` if `python3` opens the Microsoft Store.

## Steps

They run in this order. Mode `on` runs every time, `deep` only with `--deep`, `off` never.

| Step | Default | Windows | macOS | Linux | What it does |
|---|---|---|---|---|---|
| `kill_hung_apps` | deep | yes | - | - | Ends apps whose window is "Not Responding", after re-checking 10 s later |
| `stop_background_apps` | on | yes | yes | yes | Stops the helper/updater processes you list (Windows default: Widgets, Game Bar, Java and Adobe updaters) |
| `prune_orphans` | deep | yes | yes | yes | Ends dev-tool process trees (node, python, java, dotnet, ...) whose parent died, if older than 2 h |
| `restart_shell` | deep | yes | yes | - | Restarts Explorer / Finder, but only once it has grown past 600 MB |
| `trim_working_sets` | on | yes | - | - | Pushes idle pages of background apps (100 MB+) out of RAM; skips the app you're using |
| `purge_memory_caches` | deep | admin | root | root | Windows: flushes the file cache and modified list, purges the standby list. macOS: `purge`. Linux: drops page cache, compacts memory |
| `clean_temp_files` | on | yes | yes | yes | Deletes temp files, crash dumps and error reports older than 3 days |
| `flush_dns` | on | yes | root | if cached | Clears the DNS resolver cache (Linux: only if systemd-resolved or nscd runs one) |

A step that can't run on the current OS, or needs admin rights you don't have, reports `skipped` and the rest carry on.

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

`report` controls the closing summary: how many top apps to list, when to call an app heavy (1500 MB),
and how many handles counts as a likely leak (10,000, Windows only).

## Run it automatically

```
python3 system_refresh.py --install --every 4                   # standard refresh every 4 hours
python3 system_refresh.py --install --every 2 --require-idle 10  # only when you've been away 10 minutes
python3 system_refresh.py --install --deep --elevate             # deep runs with admin/root rights
python3 system_refresh.py --uninstall
```

This uses Task Scheduler on Windows, launchd on macOS and cron on Linux. `--require-idle` works on Windows and macOS.
Install as administrator/root (`--elevate`) if scheduled runs should include the admin-only steps.

## Logs

Each run writes `logs/refresh-YYYYMMDD-HHMMSS.log`, including per-process detail the console leaves out.
The newest 30 are kept (`keep_logs`).

## What to expect

The biggest wins come from ending things: hung apps, forgotten dev servers, leaky helpers. The closing report
tells you which heavy apps are left; restarting the one it names usually beats everything else.

Trimming and cache purges make "free" memory jump. But the OS was already treating that memory as reclaimable,
so apps can feel slightly slower for a moment while they page back in. That's why the purges are `deep`-only.

## Self-test

A freshly set-up machine has little to clean, so `selftest.py` creates its own bloat and checks each step
against it. It starts a memory hog, a background app that ignores the polite stop signal, orphaned process trees,
and (on Windows) a window that freezes on purpose. It also builds a sandbox of stale, fresh, read-only, in-use
and linked temp files.

```
python3 selftest.py                          # every step that can run here, about a minute
python3 selftest.py --only prune_orphans     # one step
python3 selftest.py --elevate --with-system  # also purge memory caches for real
```

It only acts on what it created. While a step runs, the tool sees only the test processes, and any attempt to stop
or trim anything else is blocked and reported as a failure. Two steps run as dry runs because they act on the whole
system: `restart_shell` always, and `purge_memory_caches` unless you add `--with-system`. Test processes end
themselves after 5 minutes even if the test is interrupted. The sandbox goes in the system temp folder and is
removed at the end.

It prints PASS, FAIL or SKIP per step, and exits with code 1 if anything failed. A step that doesn't exist on the
current OS shows as SKIP.

## Tested

Windows 11, Python 3.14, psutil 7.2: `selftest.py` passes every step across two runs.
- **As a standard user:** 8 of 9 pass; the memory purge needs admin rights and is skipped.
- **With `--elevate --with-system`:** 8 of 9 pass, including a real purge that took the standby cache from
  4.75 GB to almost nothing. `restart_shell` is skipped there by design.

Scheduling on Windows has also been checked. `--install` registered the task and one triggered run finished with
exit code 0 and a full log. That run used a dry-run config, so nothing changed. `--uninstall` then removed the task.

**Not yet run:** the macOS and Linux code paths (run `selftest.py` on those machines), scheduling through launchd
and cron, and a real Explorer or Finder restart.
