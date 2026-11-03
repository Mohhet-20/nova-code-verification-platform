"""Process-level hardening applied once at API startup.

Repository test code runs in a child process under the SAME operating-system user as the API (see testing/runner.py; it is not a sandbox). On Linux
such a process can read ``/proc/<api-pid>/environ`` and so recover NOVA_API_TOKEN and NOVA_INTEGRATION_SECRET_KEY even though the child's own
environment is stripped. Marking the API process non-dumpable makes its /proc entries root-owned, which blocks that read (and same-user ptrace).
Side effect: no core dumps and no same-user debugger attach to the API process.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger("nova.security")
PR_SET_DUMPABLE, PR_GET_DUMPABLE = 4, 3


def harden_process() -> str:
    """Return what was applied: ``nondumpable``, ``unsupported`` (not Linux) or ``failed`` (logged; startup continues)."""
    if not sys.platform.startswith("linux"):
        return "unsupported"
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 or libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")
    except (OSError, AttributeError) as exc:
        logger.warning("process_hardening_failed", extra={"error_type": type(exc).__name__})
        return "failed"
    return "nondumpable"
