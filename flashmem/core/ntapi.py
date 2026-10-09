# -*- coding: utf-8 -*-
"""
flashmem.core.ntapi — bindings ctypes das APIs nativas do Windows (NT/PSAPI/kernel32/user32).

AGENTE A do plano Flash Mem (PLANO_IMPLEMENTACAO.md seção 4).
ZERO dependências externas: apenas stdlib (ctypes, struct, os).

Fonte de definições: ntapi.h (phnt) local em
C:/Users/BAN/AppData/Local/Temp/memreduct_analysis/routine-master/src/ntapi.h

Offsets validados EMPIRICAMENTE neste host (Win10 19045 x64, Python 3.14):
- SYSTEM_PERFORMANCE_INFORMATION (retlen=344): AvailablePages@0x2C, CommittedPages@0x30,
  CommitLimit@0x34, PeakCommitment@0x38, PageReadCount@0x50 (ntapi.h:1704-1788).
- SystemMemoryListInformation query (classe 80, retlen=176 = 22 ULONG64):
  zero@0, free@8, modified@16, standby@80 — validado: zero+free+standby == AvailablePages EXATO.
- SYSTEM_PROCESS_INFORMATION (ntapi.h:1606-1645, x64): ImageName@56 (UNICODE_STRING:
  Length@56, Buffer@64), BasePriority@72, UniqueProcessId@80, WorkingSetSize@144.
- SYSTEM_PAGEFILE_INFORMATION (classe 18): NextEntryOffset@0, TotalSize@4, TotalInUse@8, PeakUsage@12, PageFileName@16.
- SYSTEM_MEMORY_LIST_COMMAND (ntapi.h:1027-1036): MemoryEmptyWorkingSets=2, MemoryFlushModifiedList=3,
  MemoryPurgeStandbyList=4, MemoryPurgeLowPriorityStandbyList=5 (valor 0 é MemoryCaptureAccessedBits).

Segurança: nenhum comando Set de limpeza global é executado pelos testes deste módulo.
Sem elevação, purge/empty retornam 0xC0000061 (STATUS_PRIVILEGED_INSTRUCTION) — fato já
validado neste host. flush_modified() checa is_elevated() e retorna status de skip quando
não elevado, pois MemoryFlushModifiedList retorna 0x0 mesmo sem elevação (sucesso != efeito).
"""

import ctypes
import os
import struct
from ctypes import wintypes

__all__ = [
    "NT_SUCCESS",
    "query_performance",
    "query_memory_lists",
    "query_pagefile",
    "enum_processes",
    "get_foreground_pid",
    "empty_working_set",
    "trim_process",
    "purge_standby",
    "flush_modified",
    "is_elevated",
    "enable_privileges",
]

# ---------------------------------------------------------------------------
# DLLs e assinaturas
# ---------------------------------------------------------------------------
_ntdll = ctypes.windll.ntdll
_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_user32 = ctypes.windll.user32
_advapi32 = ctypes.windll.advapi32

_ntdll.NtQuerySystemInformation.argtypes = [
    wintypes.ULONG,        # SystemInformationClass
    wintypes.LPVOID,       # SystemInformation
    wintypes.ULONG,        # SystemInformationLength
    ctypes.POINTER(wintypes.ULONG),  # ReturnLength
]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long  # NTSTATUS (LONG de 32 bits)

_ntdll.NtSetSystemInformation.argtypes = [
    wintypes.ULONG,
    wintypes.LPVOID,
    wintypes.ULONG,
]
_ntdll.NtSetSystemInformation.restype = ctypes.c_long

_psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
_psapi.EmptyWorkingSet.restype = wintypes.BOOL

_kernel32.SetProcessWorkingSetSize.argtypes = [
    wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t,
]
_kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

_kernel32.OpenProcess.argtypes = [
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
_kernel32.OpenProcess.restype = wintypes.HANDLE

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_kernel32.GetCurrentProcess.restype = wintypes.HANDLE

_kernel32.OpenProcessToken.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
]
_kernel32.OpenProcessToken.restype = wintypes.BOOL

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

# ---------------------------------------------------------------------------
# Classes de informação (SYSTEM_INFORMATION_CLASS, ntapi.h:355+)
# ---------------------------------------------------------------------------
SYSTEM_BASIC_INFORMATION = 0
SYSTEM_PERFORMANCE_INFORMATION = 2
SYSTEM_PROCESS_INFORMATION = 5
SYSTEM_PAGEFILE_INFORMATION = 18
SYSTEM_MEMORY_LIST_INFORMATION = 80  # ntapi.h:435 — query OK sem elevação neste host

# SYSTEM_MEMORY_LIST_COMMAND (ntapi.h:1027-1036) — valores confirmados no arquivo
MemoryCaptureAccessedBits = 0
MemoryCaptureAndResetAccessedBits = 1
MemoryEmptyWorkingSets = 2
MemoryFlushModifiedList = 3
MemoryPurgeStandbyList = 4
MemoryPurgeLowPriorityStandbyList = 5

# NTSTATUS notáveis
STATUS_SUCCESS = 0x00000000
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
STATUS_PRIVILEGED_INSTRUCTION = 0xC0000061

# Constantes de token/privilégio
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
SE_PRIVILEGE_ENABLED = 0x00000002
TokenElevation = 20
ERROR_NOT_ALL_ASSIGNED = 1300

# Status de "skip" devolvido por flush_modified() quando a sessão não é elevada.
# Valor escolhido fora do espaço NTSTATUS real para não colidir com retornos do kernel
# (NTSTATUS de falha tem bit alto setado; 0x1000… não é um código do kernel).
STATUS_SKIPPED_NOT_ELEVATED = 0x40010000  # informational customizado do Flash Mem

_PAGE_SIZE = None  # cache


def NT_SUCCESS(status: int) -> bool:
    """NT_SUCCESS: status >= 0 (bits de severidade 00/information/success)."""
    return ctypes.c_long(status & 0xFFFFFFFF).value >= 0


def _page_size() -> int:
    """PageSize via SystemBasicInformation (cacheado; constante desde o boot)."""
    global _PAGE_SIZE
    if _PAGE_SIZE is not None:
        return _PAGE_SIZE
    # SYSTEM_BASIC_INFORMATION x64 (ntapi.h:1660-1673): struct completa ocupa 64 bytes.
    # Dump empírico: [0]=0(reserved), [4]=TimerResolution, [8]=PageSize, [12]=NumberOfPhysicalPages...
    # Ou seja: PageSize@0x08 e NumberOfPhysicalPages@0x0C (ULONGs consecutivos).
    buf = ctypes.create_string_buffer(64)
    rlen = wintypes.ULONG(0)
    st = _ntdll.NtQuerySystemInformation(
        SYSTEM_BASIC_INFORMATION, buf, ctypes.sizeof(buf), ctypes.byref(rlen)
    )
    if not NT_SUCCESS(st):
        raise OSError(st & 0xFFFFFFFF, "NtQuerySystemInformation(SystemBasicInformation) falhou")
    _PAGE_SIZE = struct.unpack_from("<I", buf.raw, 0x08)[0]
    return _PAGE_SIZE


def query_performance() -> dict:
    """Contadores de SystemPerformanceInformation (layout ntapi.h:1704-1788, x64, 344 bytes).

    Retorna dict com:
      available_pages, committed_pages, commit_limit, peak_commit,
      page_read_count (hard faults cumulativos), page_size
    """
    buf = ctypes.create_string_buffer(344)
    rlen = wintypes.ULONG(0)
    st = _ntdll.NtQuerySystemInformation(
        SYSTEM_PERFORMANCE_INFORMATION, buf, 344, ctypes.byref(rlen)
    )
    if not NT_SUCCESS(st):
        raise OSError(st & 0xFFFFFFFF, "NtQuerySystemInformation(SystemPerformanceInformation) falhou")
    raw = buf.raw
    return {
        # offsets x64 confirmados empiricamente (cabeçalho: 4 LARGE_INTEGER + 3 ULONG = 44 bytes)
        "available_pages": struct.unpack_from("<I", raw, 0x2C)[0],
        "committed_pages": struct.unpack_from("<I", raw, 0x30)[0],
        "commit_limit": struct.unpack_from("<I", raw, 0x34)[0],
        "peak_commit": struct.unpack_from("<I", raw, 0x38)[0],
        "page_read_count": struct.unpack_from("<I", raw, 0x50)[0],
        "page_size": _page_size(),
    }


def query_memory_lists() -> dict | None:
    """Listas de PFN via query SystemMemoryListInformation (classe 80).

    Retorna dict com contadores em PÁGINAS: {"zero","free","modified","standby"}.
    None se a query falhar (ex.: versão de Windows sem suporte).

    Layout empírico neste host (Win10 19045 x64): retorno de 176 bytes = 22 ULONG64,
    com zero@0, free@8, modified@16. Validado em N=10 amostras: o standby TOTAL é
    q[10] + bandas de prioridade q[5..9] (zero+free+Σ == AvailablePages com diff
    0,0000%); usar apenas q[10] subestima a standby em ~30% neste host. O oráculo
    (d) do test_ntapi (zero+free+standby ≈ available ±8%) é a prova de contrato.
    """
    buf = ctypes.create_string_buffer(256)
    rlen = wintypes.ULONG(0)
    st = _ntdll.NtQuerySystemInformation(
        SYSTEM_MEMORY_LIST_INFORMATION, buf, 256, ctypes.byref(rlen)
    )
    if not NT_SUCCESS(st) or rlen.value < 88:
        return None
    q = struct.unpack_from("<%dQ" % (rlen.value // 8), buf.raw, 0)
    # standby TOTAL = Σ PageCountByPriority[0..7] (qwords q[5..12]).
    # Validado empiricamente N=10 neste host: zero+free+Σ(q[5..12]) ==
    # AvailablePages EXATO (diff 0.0000%), enquanto o q[10]-only antigo
    # subestimava ~30% (e violava o oráculo ±8% do test_ntapi).
    # Referência cruzada: ProcessHacker/SystemInformer memlists.c somam
    # PageCountByPriority[0..7] como standby total — mesmo layout (Zero,
    # Free, Modified, ModifiedNoWrite, Bad, Prio[8], Repurposed[8], ...).
    if len(q) >= 13:
        standby = sum(q[5:13])
    else:  # build antigo: fallback tolerante ao único qword conhecido
        standby = q[10]
    return {
        "zero": q[0],
        "free": q[1],
        "modified": q[2],
        "standby": standby,
    }


def query_pagefile() -> dict:
    """Uso de pagefile via SystemPageFileInformation (classe 18).

    Percorre a lista encadeada (NextEntryOffset) somando TotalSize/TotalInUse.
    Retorna {"used_bytes": int, "limit_bytes": int} (bytes).
    """
    size = 0x400
    for _ in range(6):
        buf = ctypes.create_string_buffer(size)
        rlen = wintypes.ULONG(0)
        st = _ntdll.NtQuerySystemInformation(
            SYSTEM_PAGEFILE_INFORMATION, buf, size, ctypes.byref(rlen)
        )
        if (st & 0xFFFFFFFF) == STATUS_INFO_LENGTH_MISMATCH:
            size *= 2
            continue
        if not NT_SUCCESS(st):
            raise OSError(st & 0xFFFFFFFF, "NtQuerySystemInformation(SystemPageFileInformation) falhou")
        used = 0
        total = 0
        off = 0
        while True:
            nxt = struct.unpack_from("<I", buf.raw, off)[0]
            total += struct.unpack_from("<I", buf.raw, off + 4)[0]   # TotalSize (páginas)
            used += struct.unpack_from("<I", buf.raw, off + 8)[0]   # TotalInUse (páginas)
            if nxt == 0:
                break
            off += nxt
        ps = _page_size()
        return {"used_bytes": used * ps, "limit_bytes": total * ps}
    raise OSError(STATUS_INFO_LENGTH_MISMATCH, "buffer de pagefile insuficiente após 6 tentativas")


def enum_processes() -> list[dict]:
    """Enumera processos via NtQuerySystemInformation(SystemProcessInformation=5).

    Buffer crescente (dobrando até 8 vezes) para absorver STATUS_INFO_LENGTH_MISMATCH.
    Retorna lista de dicts: {"pid": int, "name": str, "ws_bytes": int, "priority": int}.
    Nome vem do UNICODE_STRING ImageName do próprio processo (sem caminho).
    """
    size = 0x100000
    procs = None
    st = 0
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        rlen = wintypes.ULONG(0)
        st = _ntdll.NtQuerySystemInformation(
            SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(rlen)
        )
        if (st & 0xFFFFFFFF) == STATUS_INFO_LENGTH_MISMATCH:
            size *= 2
            continue
        if not NT_SUCCESS(st):
            raise OSError(st & 0xFFFFFFFF, "NtQuerySystemInformation(SystemProcessInformation) falhou")
        procs = []
        raw = buf.raw
        off = 0
        while True:
            nxt = struct.unpack_from("<I", raw, off)[0]
            # Offsets x64 (ntapi.h:1606-1645) confirmados empiricamente:
            pid = struct.unpack_from("<Q", raw, off + 80)[0]
            prio = struct.unpack_from("<i", raw, off + 72)[0]
            ws = struct.unpack_from("<Q", raw, off + 144)[0]
            ulen = struct.unpack_from("<H", raw, off + 56)[0]
            ubuf = struct.unpack_from("<Q", raw, off + 64)[0]
            name = ""
            if ulen and ubuf:
                try:
                    name = ctypes.string_at(ubuf, ulen).decode("utf-16-le", "replace")
                except OSError:
                    name = ""
            procs.append({"pid": pid, "name": name, "ws_bytes": ws, "priority": prio})
            if nxt == 0:
                break
            off += nxt
        break
    if procs is None:
        raise OSError(st & 0xFFFFFFFF, "enum_processes: buffer insuficiente")
    return procs


def get_foreground_pid() -> int | None:
    """PID da janela em primeiro plano (GetForegroundWindow + GetWindowThreadProcessId)."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) if pid.value else None


def _open_process_for_trim(pid: int) -> wintypes.HANDLE:
    """Abre handle com direitos para trim (PROCESS_QUERY_LIMITED_INFORMATION|SET_QUOTA)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_SET_QUOTA = 0x0100
    h = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid
    )
    return h


def empty_working_set(pid: int) -> int:
    """EmptyWorkingSet (psapi) no processo `pid`. Retorna NTSTATUS (0 = sucesso).

    Requer PROCESS_SET_QUOTA; para o próprio processo usa o pseudo-handle.
    """
    if pid == os.getpid():
        h = _kernel32.GetCurrentProcess()
        ok = bool(_psapi.EmptyWorkingSet(h))
        return STATUS_SUCCESS if ok else (0xC0000005 if not ok else 0)
    h = _open_process_for_trim(pid)
    if not h:
        return 0xC0000022  # STATUS_ACCESS_DENIED
    try:
        ok = bool(_psapi.EmptyWorkingSet(h))
        return STATUS_SUCCESS if ok else 0xC0000001  # STATUS_UNSUCCESSFUL
    finally:
        _kernel32.CloseHandle(h)


def trim_process(pid: int) -> int:
    """SetProcessWorkingSetSize(h, -1, -1) — fallback de trim (como EmptyWorkingSet)."""
    if pid == os.getpid():
        h = _kernel32.GetCurrentProcess()
        ok = bool(_kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1)))
        return STATUS_SUCCESS if ok else 0xC0000001
    h = _open_process_for_trim(pid)
    if not h:
        return 0xC0000022  # STATUS_ACCESS_DENIED
    try:
        ok = bool(
            _kernel32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        )
        return STATUS_SUCCESS if ok else 0xC0000001
    finally:
        _kernel32.CloseHandle(h)


def purge_standby(low_only: bool = True) -> int:
    """NtSetSystemInformation(SystemMemoryListInformation, comando de purge).

    ATENÇÃO: mutação global de sistema. Sem elevação retorna 0xC0000061 neste host
    (fato validado empiricamente). Chamadores devem tratar o NTSTATUS, não silenciá-lo.
    """
    cmd = wintypes.ULONG(MemoryPurgeLowPriorityStandbyList if low_only else MemoryPurgeStandbyList)
    st = _ntdll.NtSetSystemInformation(
        SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(cmd), ctypes.sizeof(cmd)
    )
    return st & 0xFFFFFFFF


def flush_modified() -> int:
    """Flush da modified list (MemoryFlushModifiedList).

    Fato empírico deste host: MemoryFlushModifiedList retorna 0x0 MESMO sem elevação
    (sucesso da syscall != efeito real). Para não criar falso positivo, retornamos
    STATUS_SKIPPED_NOT_ELEVATED quando a sessão não é elevada, sem chamar a syscall.
    """
    if not is_elevated():
        return STATUS_SKIPPED_NOT_ELEVATED
    cmd = wintypes.ULONG(MemoryFlushModifiedList)
    st = _ntdll.NtSetSystemInformation(
        SYSTEM_MEMORY_LIST_INFORMATION, ctypes.byref(cmd), ctypes.sizeof(cmd)
    )
    return st & 0xFFFFFFFF


def is_elevated() -> bool:
    """True se o token do processo atual está elevado (TokenElevation via advapi32)."""
    h = _kernel32.GetCurrentProcess()
    tok = wintypes.HANDLE()
    if not _kernel32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
        return False
    try:
        _advapi32.GetTokenInformation.restype = wintypes.BOOL
        _advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        val = wintypes.ULONG(0)
        ret = wintypes.DWORD(0)
        if not _advapi32.GetTokenInformation(
            tok, TokenElevation, ctypes.byref(val), 4, ctypes.byref(ret)
        ):
            return False
        return bool(val.value)
    finally:
        _kernel32.CloseHandle(tok)


def enable_privileges() -> list[str]:
    """Tenta habilitar SeProfileSingleProcessPrivilege e SeIncreaseQuotaPrivilege.

    Retorna a lista dos privilégios efetivamente habilitados. Sessão não elevada:
    AdjustTokenPrivileges "succeede" mas GetLastError()==1300 (ERROR_NOT_ALL_ASSIGNED)
    — retornamos lista vazia e o chamador decide (ex.: skip de purge).
    """
    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class TOKEN_PRIVILEGES_1(ctypes.Structure):
        _fields_ = [
            ("PrivilegeCount", wintypes.DWORD),
            ("Luid", LUID),
            ("Attributes", wintypes.DWORD),
        ]

    _advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID),
    ]
    _advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(LUID),
    ]

    names = ("SeProfileSingleProcessPrivilege", "SeIncreaseQuotaPrivilege")
    h = _kernel32.GetCurrentProcess()
    tok = wintypes.HANDLE()
    if not _kernel32.OpenProcessToken(
        h, TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(tok)
    ):
        return []
    enabled = []
    try:
        for name in names:
            luid = LUID()
            if not _advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                continue
            tp = TOKEN_PRIVILEGES_1(1, luid, SE_PRIVILEGE_ENABLED)
            _advapi32.AdjustTokenPrivileges(
                tok, False, ctypes.byref(tp), 0, None, None
            )
            if _kernel32.GetLastError() != ERROR_NOT_ALL_ASSIGNED:
                enabled.append(name)
    finally:
        _kernel32.CloseHandle(tok)
    return enabled
