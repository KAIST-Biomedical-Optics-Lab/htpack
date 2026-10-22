"""Operating-system adapters; compression packets stay platform-independent."""
import os
from pathlib import Path
import sys
import psutil

WINDOWS=sys.platform=="win32"


def peak_rss():
    if WINDOWS:
        # psutil's Windows peak working set is the peak of the same metric
        # exposed as current RSS by memory_info().rss. Never substitute current
        # RSS for an unavailable peak measurement.
        return int(psutil.Process().memory_info().peak_wset)
    import resource
    value=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform=="darwin" else value*1024)


def publish_no_overwrite(temporary,target):
    temporary=Path(temporary);target=Path(target)
    if WINDOWS:
        # os.rename on Windows rejects an existing destination. Using rename
        # also avoids requiring hard-link support on the output filesystem.
        os.rename(temporary,target)
    else:
        # POSIX rename would overwrite; hard-link publication must fail if a
        # file appeared after our initial existence check.
        os.link(temporary,target)


def configure_console():
    if WINDOWS:
        for stream in (sys.stdout,sys.stderr):
            configure=getattr(stream,"reconfigure",None)
            if configure is not None:
                configure(encoding="utf-8",errors="backslashreplace")

