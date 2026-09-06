"""Toggleable fault injectors for the demo services."""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

# Fault state is persisted outside the process so a container restart does not
# silently cure every fault (which would make "restart didn't help" impossible).
# `docker restart` keeps the writable layer, so a file under /tmp survives it.
_STATE_PATH = Path(
    os.getenv("FAULT_STATE_PATH", str(Path(tempfile.gettempdir()) / "incident_fault_mode"))
)

# Faults a restart genuinely cures: their damage is leaked in-process resources,
# which the fresh process no longer holds. Code/config faults survive a restart
# because the bad release is still the one running.
RESTART_CLEARED_FAULTS = {"db_pool_exhaustion", "memory_leak"}

# ~500 MB ceiling on the simulated leak.
MEMORY_BALLAST_MAX_CHUNKS = int(os.getenv("FAULT_MEMORY_MAX_CHUNKS", "100"))

_fault_lock = threading.Lock()
_leaked_connections: list = []
_memory_ballast: list = []


def _persist(mode: str) -> None:
    try:
        _STATE_PATH.write_text(mode, encoding="utf-8")
    except Exception:
        # Demo must still run on a read-only or unusual filesystem
        pass


def _load_persisted_fault() -> str:
    try:
        mode = _STATE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return os.getenv("FAULT_MODE", "none") or "none"
    if not mode or mode in RESTART_CLEARED_FAULTS:
        # This process is fresh, so the leaked resources are gone with it.
        _persist("none")
        return "none"
    return mode


_active_fault: str = _load_persisted_fault()
os.environ["FAULT_MODE"] = _active_fault


def get_fault() -> str:
    with _fault_lock:
        return _active_fault or "none"


def set_fault(mode: str) -> str:
    global _active_fault
    with _fault_lock:
        _active_fault = mode
        os.environ["FAULT_MODE"] = mode
        _persist(mode)
        return _active_fault


def clear_fault() -> str:
    global _active_fault, _leaked_connections, _memory_ballast
    with _fault_lock:
        _leaked_connections = []
        _memory_ballast = []
        _active_fault = "none"
        os.environ["FAULT_MODE"] = _active_fault
        _persist(_active_fault)
        return _active_fault


def apply_fault(service: str = "") -> Optional[Exception]:
    """
    Apply the currently active fault.
    Returns an Exception to raise, or None if request should continue.
    Side-effect faults (latency, memory, leak) mutate process state.
    """
    mode = get_fault()
    if mode in ("none", "", None):
        return None

    if mode == "exception":
        return RuntimeError(f"[{service}] Injected application exception")

    if mode == "http_500":
        return RuntimeError(f"[{service}] Injected HTTP 500 storm")

    if mode == "latency":
        time.sleep(float(os.getenv("FAULT_LATENCY_SECONDS", "2.5")))
        return None

    if mode == "db_timeout":
        time.sleep(float(os.getenv("FAULT_DB_TIMEOUT_SECONDS", "3.0")))
        return TimeoutError(f"[{service}] Database connection timeout")

    if mode == "db_pool_exhaustion":
        # Simulate leaking connections
        for _ in range(20):
            _leaked_connections.append(object())
        return TimeoutError(
            f"[{service}] ConnectionPoolTimeout: pool at 100% capacity "
            f"(in_use={len(_leaked_connections)})"
        )

    if mode == "memory_leak":
        # Continuously allocate ~5MB, up to a bounded ceiling
        if len(_memory_ballast) < MEMORY_BALLAST_MAX_CHUNKS:
            _memory_ballast.append(bytearray(5 * 1024 * 1024))
        return None

    if mode == "config_error":
        return ValueError(
            f"[{service}] Configuration mismatch: invalid DATABASE_URL / API key"
        )

    if mode == "bad_deploy":
        # Mimic a TypeError introduced by a bad release
        return TypeError(
            f"[{service}] TypeError in payment processing: "
            "unexpected NoneType for amount (introduced in bad deploy)"
        )

    if mode == "dependency_failure":
        return ConnectionError(f"[{service}] Upstream dependency unavailable")

    return None


def leaked_connection_count() -> int:
    return len(_leaked_connections)


def memory_ballast_bytes() -> int:
    return sum(len(b) for b in _memory_ballast)
