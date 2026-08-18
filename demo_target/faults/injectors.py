"""Toggleable fault injectors for the demo services."""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

# Shared process-local fault state (can also be driven by FAULT_MODE env)
_fault_lock = threading.Lock()
_active_fault: str = os.getenv("FAULT_MODE", "none")
_leaked_connections: list = []
_memory_ballast: list = []


def get_fault() -> str:
    with _fault_lock:
        return _active_fault or "none"


def set_fault(mode: str) -> str:
    global _active_fault
    with _fault_lock:
        _active_fault = mode
        os.environ["FAULT_MODE"] = mode
        return _active_fault


def clear_fault() -> str:
    global _leaked_connections, _memory_ballast
    with _fault_lock:
        _leaked_connections = []
        _memory_ballast = []
        return set_fault("none")


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
        # Continuously allocate ~5MB
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
