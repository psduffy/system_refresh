"""--install, --uninstall and --elevate: running system_refresh on a schedule or with admin rights."""

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

from .common import DEFAULT_CONFIG_PATH, IS_MAC, IS_WINDOWS, PSUTIL_DIR, SCRIPT_DIR, SCRIPT_PATH, is_admin, run

TASK_NAME = "SystemRefresh"
LAUNCHD_LABEL = "com.systemrefresh"
CRON_MARKER = "# system_refresh"


def relaunch_elevated(argv, script=SCRIPT_PATH):
    forwarded = [arg for arg in argv if arg != "--elevate"]
    if IS_WINDOWS:
        from .windows import run_as_admin
        result = run_as_admin(sys.executable, subprocess.list2cmdline([script] + forwarded + ["--pause"]))
        if result <= 32:
            print("Elevation was cancelled or failed (code %d)." % result)
            return 1
        print("Continuing in the administrator window.")
        return 0
    # sudo resets the environment, so point root's Python at the psutil this user installed.
    command = ["sudo", "env", "PYTHONPATH=" + PSUTIL_DIR, sys.executable, script] + forwarded
    os.execvp("sudo", command)


TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Runs system_refresh.py to prune resource bloat.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_windows(flags, minutes):
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    command = pythonw if os.path.exists(pythonw) else sys.executable
    domain = os.environ.get("USERDOMAIN")
    user = "%s\\%s" % (domain, getpass.getuser()) if domain else getpass.getuser()
    xml = TASK_XML.format(
        minutes=minutes,
        start=(datetime.now() + timedelta(minutes=5)).replace(microsecond=0).isoformat(),
        user=escape(user),
        run_level="HighestAvailable" if is_admin() else "LeastPrivilege",
        command=escape(command),
        arguments=escape(subprocess.list2cmdline([SCRIPT_PATH] + flags)),
        workdir=escape(SCRIPT_DIR))
    path = os.path.join(tempfile.gettempdir(), "system_refresh_task.xml")
    with open(path, "w", encoding="utf-16") as target:
        target.write(xml)
    try:
        code, out = run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", path])
    finally:
        os.remove(path)
    print(out)
    return code


def launchd_location():
    """(plist path, launchd domain): a daemon when installing as root, otherwise a user agent."""
    if os.geteuid() == 0:
        return "/Library/LaunchDaemons/%s.plist" % LAUNCHD_LABEL, "system"
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LAUNCHD_LABEL), "gui/%d" % os.getuid()


def install_mac(flags, minutes):
    import plistlib
    path, domain = launchd_location()
    job = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, SCRIPT_PATH] + flags,
        "EnvironmentVariables": {"PYTHONPATH": PSUTIL_DIR},
        "StartInterval": minutes * 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])  # replace an earlier install
    with open(path, "wb") as target:
        plistlib.dump(job, target)
    code, out = run(["launchctl", "bootstrap", domain, path])
    print(out or "Loaded %s into launchd (%s)." % (path, domain))
    return code


def install_cron(flags, minutes):
    if minutes < 60:
        schedule = "*/%d * * * *" % minutes
    elif minutes < 24 * 60:
        schedule = "0 */%d * * *" % max(1, round(minutes / 60))
    else:
        schedule = "0 3 */%d * *" % max(1, round(minutes / (24 * 60)))
    command = " ".join(shlex.quote(part) for part in [sys.executable, SCRIPT_PATH] + flags)
    line = "%s PYTHONPATH=%s %s >/dev/null 2>&1 %s" % (schedule, shlex.quote(PSUTIL_DIR), command, CRON_MARKER)
    code, current = run(["crontab", "-l"])
    lines = [kept for kept in current.splitlines() if CRON_MARKER not in kept] if code == 0 else []
    result = subprocess.run(["crontab", "-"], input="\n".join(lines + [line]) + "\n",
                            universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout.strip() or "Added to %s's crontab: %s" % (getpass.getuser(), schedule))
    return result.returncode


def install(args, config_path):
    flags = ["--quiet"]
    if args.deep:
        flags.append("--deep")
    if args.require_idle:
        flags += ["--require-idle", "%g" % args.require_idle]
    if config_path != DEFAULT_CONFIG_PATH:
        flags += ["--config", config_path]
    if not IS_WINDOWS and os.geteuid() == 0:
        target = args.target_user or os.environ.get("SUDO_USER")
        if target:
            flags += ["--target-user", target]
    minutes = max(1, int(round(args.every * 60)))
    if IS_WINDOWS:
        return install_windows(flags, minutes)
    if IS_MAC:
        return install_mac(flags, minutes)
    return install_cron(flags, minutes)


def uninstall():
    if IS_WINDOWS:
        if run(["schtasks", "/Query", "/TN", TASK_NAME])[0] != 0:
            print("No scheduled run was installed.")
            return 0
        code, out = run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        print(out)
        return code
    if IS_MAC:
        path, domain = launchd_location()
        run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])
        if not os.path.exists(path):
            print("No scheduled run was installed.")
            return 0
        os.remove(path)
        print("Removed %s." % path)
        return 0
    code, current = run(["crontab", "-l"])
    lines = current.splitlines() if code == 0 else []
    kept = [line for line in lines if CRON_MARKER not in line]
    if len(kept) == len(lines):
        print("No scheduled run was installed.")
        return 0
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n" if kept else "", universal_newlines=True)
    print("Removed the system_refresh line from the crontab.")
    return 0
