#!/usr/bin/env python3
"""
system_refresh: prune resource bloat on a running machine, the next best thing to a reboot.

Runs on Windows 10/11, macOS and Linux (RHEL 8+). Needs Python 3.6+ and psutil.

It works toward eight goals. For each one it finds the best way this machine offers (see --explain):

  kill_hung_apps        end apps that stay "Not Responding"
  stop_background_apps  stop the background apps you approved with --discover
  prune_orphans         end dev-tool process trees whose parent has died
  restart_shell         restart Explorer or Finder once it has bloated
  release_idle_memory   get background apps to give back idle memory
  purge_memory_caches   drop the standby list or disk cache
  clean_temp_files      delete stale temp files, crash dumps and error reports
  flush_dns             clear the DNS resolver cache

Examples:
  python3 system_refresh.py --explain            what this machine can do, and how
  python3 system_refresh.py --discover           pick background apps to stop on every run
  python3 system_refresh.py --dry-run            show what a standard run would do
  python3 system_refresh.py --deep --elevate     everything, as administrator / root
  python3 system_refresh.py --install --every 4 --require-idle 10
"""

import sys

from refresh.cli import main

if __name__ == "__main__":
    sys.exit(main())
