#!/usr/bin/env python3
"""Run one shell command in a dedicated process group (macOS / Windows)."""

from __future__ import annotations

import os
import platform
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: subfix_process_group.py COMMAND", file=sys.stderr)
        return 2
    if os.name == "nt" or platform.system() == "Windows":
        # Windows 下没有 setsid / /bin/sh；用新进程组启动 cmd /c，便于后续按 PID 终止。
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            ["cmd.exe", "/c", arguments[0]],
            creationflags=creation_flags,
            close_fds=True,
        )
        return 0
    os.setsid()
    os.execl("/bin/sh", "sh", "-c", arguments[0])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
