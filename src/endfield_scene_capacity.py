"""Read available physical memory without adding runtime dependencies."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass


class MemoryProbeUnavailable(RuntimeError):
    """Raised when the host cannot report available physical memory."""


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PerformanceInformation(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), *( (name, ctypes.c_size_t) for name in (
        "CommitTotal", "CommitLimit", "CommitPeak", "PhysicalTotal", "PhysicalAvailable",
        "SystemCache", "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize")),
        ("HandleCount", ctypes.c_ulong), ("ProcessCount", ctypes.c_ulong), ("ThreadCount", ctypes.c_ulong)]


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
               *((name, ctypes.c_size_t) for name in (
                   "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                   "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                   "PeakPagefileUsage", "PrivateUsage"))]


@dataclass(frozen=True)
class SystemMemorySnapshot:
    available_physical_bytes: int
    total_physical_bytes: int
    available_commit_bytes: int
    total_commit_bytes: int


@dataclass(frozen=True)
class ProcessMemorySnapshot:
    working_set_bytes: int
    peak_working_set_bytes: int
    private_commit_bytes: int
    peak_commit_bytes: int


@dataclass(frozen=True)
class ActiveMemoryReservation:
    budget_bytes: int
    current: ProcessMemorySnapshot


@dataclass(frozen=True)
class MemoryAdmission:
    allowed: bool
    physical_required_bytes: int
    commit_required_bytes: int
    available_physical_bytes: int
    available_commit_bytes: int


def system_memory_snapshot() -> SystemMemorySnapshot:
    """Read physical availability and the system commit limit independently."""
    if os.name != "nt":
        raise MemoryProbeUnavailable(f"Physical/commit admission requires Windows: os.name={os.name!r}; select one scene worker")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.GlobalMemoryStatusEx
    query.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
    query.restype = ctypes.c_bool
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    if not query(ctypes.byref(status)):
        error = ctypes.get_last_error()
        raise MemoryProbeUnavailable(f"GlobalMemoryStatusEx failed: windows_error={error}") from ctypes.WinError(error)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    performance = psapi.GetPerformanceInfo
    performance.argtypes = [ctypes.POINTER(_PerformanceInformation), ctypes.c_ulong]
    performance.restype = ctypes.c_bool
    counters = _PerformanceInformation()
    counters.cb = ctypes.sizeof(counters)
    if not performance(ctypes.byref(counters), counters.cb):
        error = ctypes.get_last_error()
        raise MemoryProbeUnavailable(f"GetPerformanceInfo failed: windows_error={error}") from ctypes.WinError(error)
    if counters.CommitTotal > counters.CommitLimit or counters.PageSize <= 0 or status.ullTotalPhys <= 0:
        raise MemoryProbeUnavailable("Windows returned invalid physical/commit counters")
    return SystemMemorySnapshot(int(status.ullAvailPhys), int(status.ullTotalPhys),
                                int((counters.CommitLimit - counters.CommitTotal) * counters.PageSize),
                                int(counters.CommitLimit * counters.PageSize))


def process_memory_snapshot(process_handle: int) -> ProcessMemorySnapshot:
    """Read an owned live Windows process handle without opening unrelated processes."""
    if os.name != "nt" or process_handle <= 0:
        raise MemoryProbeUnavailable(f"Process memory requires a positive Windows process handle: os.name={os.name!r}, handle={process_handle}")
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    query = psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessMemoryCountersEx), ctypes.c_ulong]
    query.restype = ctypes.c_bool
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    if not query(ctypes.c_void_p(process_handle), ctypes.byref(counters), counters.cb):
        error = ctypes.get_last_error()
        raise MemoryProbeUnavailable(f"GetProcessMemoryInfo failed: windows_error={error}, handle={process_handle}") from ctypes.WinError(error)
    return ProcessMemorySnapshot(int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize),
                                 int(counters.PrivateUsage), int(counters.PeakPagefileUsage))


def memory_admission(candidate_bytes: int, active: tuple[ActiveMemoryReservation, ...],
                     system: SystemMemorySnapshot, reserve_bytes: int) -> MemoryAdmission:
    """Reserve future growth only; live availability already includes resident children."""
    if candidate_bytes <= 0 or reserve_bytes < 0 or any(row.budget_bytes <= 0 for row in active):
        raise ValueError("Admission requires positive scene budgets and a nonnegative system reserve")
    physical = candidate_bytes + reserve_bytes + sum(
        max(0, row.budget_bytes - row.current.working_set_bytes) for row in active)
    commit = candidate_bytes + reserve_bytes + sum(
        max(0, row.budget_bytes - row.current.private_commit_bytes) for row in active)
    # A growing child that has exceeded its reservation stops further admission.
    exceeded = any(max(row.current.working_set_bytes, row.current.private_commit_bytes,
                       row.current.peak_working_set_bytes, row.current.peak_commit_bytes) > row.budget_bytes for row in active)
    return MemoryAdmission(not exceeded and physical <= system.available_physical_bytes and commit <= system.available_commit_bytes,
                           physical, commit, system.available_physical_bytes, system.available_commit_bytes)


def available_physical_memory_bytes() -> int:
    """Return the OS-reported available physical memory or fail explicitly."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.GlobalMemoryStatusEx
        query.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        query.restype = ctypes.c_bool
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if not query(ctypes.byref(status)):
            error = ctypes.get_last_error()
            raise MemoryProbeUnavailable(
                f"GlobalMemoryStatusEx failed with Windows error {error}; set ENDFIELD_SCENE_WORKERS=1 to run without parallel admission"
            ) from ctypes.WinError(error)
        available = int(status.ullAvailPhys)
    elif hasattr(os, "sysconf"):
        try:
            pages = int(os.sysconf("SC_AVPHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError) as error:
            raise MemoryProbeUnavailable(
                f"sysconf could not report available physical memory: {error}; set ENDFIELD_SCENE_WORKERS=1 to run without parallel admission"
            ) from error
        if pages <= 0 or page_size <= 0:
            raise MemoryProbeUnavailable(
                f"sysconf returned invalid available-memory values: pages={pages}, page_size={page_size}"
            )
        available = pages * page_size
    else:
        raise MemoryProbeUnavailable(
            f"No standard-library physical-memory probe is available: os.name={os.name!r}"
        )
    if available <= 0:
        raise MemoryProbeUnavailable(
            f"Operating system reported invalid available physical memory: bytes={available}"
        )
    return available
