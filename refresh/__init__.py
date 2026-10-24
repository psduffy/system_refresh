"""system_refresh: goals for pruning resource bloat, and the ways each OS can reach them.

common.py    settings, logging, the run context and process helpers
goals.py     the goals, the ways that work on every OS, and how one is chosen per machine
windows.py   Windows ways and facts (Win32 calls through ctypes)
macos.py     macOS ways and facts
linux.py     Linux ways and facts
discover.py  --discover: finding background apps worth stopping, approved once
report.py    the before/after snapshot and the heavy-app report
schedule.py  --install, --uninstall and --elevate
cli.py       the command line
"""
