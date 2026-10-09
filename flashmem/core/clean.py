# -*- coding: utf-8 -*-
"""
flashmem.core.clean — limpeza seletiva com dry-run (AGENTE D, Onda 2).

Filosofia (PLANO_IMPLEMENTACAO.md seção 3, clean.py):
- Nunca aparar o processo em primeiro plano (janela com foco do usuário).
- Nunca aparar processos críticos do sistema (EXCLUDE_NAMES) nem o próprio
  agente (os.getpid()).
- dry-run é OBRIGATÓRIO por padrão: sem mutação de sistema sem flag explícita.
- Erros de syscall NUNCA silenciosos: todo NTSTATUS registrado no CleanAction.
- freed_accounting é CONTÁBIL (físico usado antes - depois): pode até ser
  NEGATIVO (páginas migraram para standby; continuam na RAM) — preservamos
  o valor real, sem clamp para 0.

snapshot antes/depois: measure.snapshot() quando existir; fallback via
ntapi.query_performance() (available_pages*page_size → physical_used),
documentado em _take_snapshot_fallback().
"""

import os
import time
from dataclasses import dataclass, field

from flashmem.core import ntapi
from flashmem.core.config import ProfileConfig

__all__ = [
    "CleanAction",
    "CleanReport",
    "EXCLUDE_NAMES",
    "clean",
    "pick_targets",
]

# ---------------------------------------------------------------------------
# Import suave de measure.py (AGENTE B, paralelo): pode ainda não existir.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - ramo dependente do ambiente
    from flashmem.core.measure import snapshot as _measure_snapshot
except Exception:  # ImportError ou erro transitório na importação
    _measure_snapshot = None

_MB = 1024 * 1024

# Nomes EXATOS do PLANO_IMPLEMENTACAO.md seção 3 (comparados em lowercase).
EXCLUDE_NAMES = {
    "csrss.exe", "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe",
    "dwm.exe", "system", "registry", "memory compression", "explorer.exe",
    "fontdrvhost.exe", "svchost.exe", "conhost.exe", "python.exe",
    "pythonw.exe", "hermes",
}

# Status de skip usado por flush_modified() quando a sessão não é elevada
# (espelha ntapi.STATUS_SKIPPED_NOT_ELEVATED para não criar dependência frágil).
_STATUS_SKIPPED_NOT_ELEVATED = 0x40010000


@dataclass
class CleanAction:
    """Uma ação de limpeza (executada ou apenas planejada em dry-run)."""
    kind: str      # "trim_process"|"purge_standby_low"|"purge_standby_full"|"flush_modified"
    target: str    # "pid:nome" ou "sistema"
    status: str    # "ok"|"skipped"|"failed"
    ntstatus: int  # 0 se n/a
    detail: str    # pt-BR


@dataclass
class CleanReport:
    """Resultado de clean(): ações + delta contábil + duração."""
    dry_run: bool
    actions: list[CleanAction] = field(default_factory=list)
    freed_accounting: int = 0  # bytes; pode ser NEGATIVO (ver docstring módulo)
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Snapshot antes/depois
# ---------------------------------------------------------------------------

def _snapshot_dict() -> dict:
    """Snapshot mínimo {"physical_used": int} em bytes.

    Usa measure.snapshot() quando disponível (campo physical_used real).
    Fallback documentado (measure.py ainda ausente): query_performance()
    expõe apenas available_pages, e físico TOTAL não é derivável dele;
    usamos o proxy physical_used = -available_bytes. O DELTA antes/depois
    continua correto em sinal e magnitude, pois
    (used_before - used_after) == (avail_after - avail_before); apenas a
    âncora absoluta é proxy até o AGENTE B entregar measure.snapshot().
    """
    if _measure_snapshot is not None:
        snap = _measure_snapshot()
        return {"physical_used": int(snap.physical_used)}
    perf = ntapi.query_performance()
    avail_bytes = perf["available_pages"] * perf["page_size"]
    # Proxy documentado: físico "usado" com sinal invertido do disponível.
    return {"physical_used": -avail_bytes}


def _used_bytes(snap: dict) -> int:
    return int(snap.get("physical_used", 0))


# ---------------------------------------------------------------------------
# Seleção de alvos
# ---------------------------------------------------------------------------

def pick_targets(profile: ProfileConfig, processes: list[dict],
                 foreground_pid: int | None,
                 self_pid: int | None = None) -> list[dict]:
    """Filtra e ordena candidatos; devolve top trim_top por WS desc.

    Regras (PLANO seção 3): ws_bytes >= min_ws_mb MB; nome (lowercase) fora
    de EXCLUDE_NAMES; pid != foreground; pid != self_pid (default os.getpid()).
    """
    self_pid = os.getpid() if self_pid is None else self_pid
    min_bytes = profile.min_ws_mb * _MB
    out = []
    for proc in processes:
        if proc.get("pid") in (foreground_pid, self_pid):
            continue
        name = str(proc.get("name", "")).strip().lower()
        if name in EXCLUDE_NAMES:
            continue
        if proc.get("ws_bytes", 0) < min_bytes:
            continue
        out.append(proc)
    out.sort(key=lambda p: p.get("ws_bytes", 0), reverse=True)
    return out[: max(0, int(profile.trim_top))]


# ---------------------------------------------------------------------------
# clean()
# ---------------------------------------------------------------------------

def clean(profile: ProfileConfig, dry_run: bool = True,
          before: "object | None" = None) -> CleanReport:
    """Executa (ou planeja, em dry-run) a limpeza seletiva do perfil.

    Fluxo: enum_processes → filtro/top-N → EmptyWorkingSet (fallback
    trim_process) por alvo → purge_standby/flush_modified conforme perfil
    → snapshot final → freed_accounting = before_used - after_used.

    Segurança: dry_run=True (default) NUNCA muta estado; nenhuma syscall
    Set/Empty é chamada. Foreground, EXCLUDE_NAMES e o próprio pid nunca
    são alvo.
    """
    t0 = time.perf_counter()
    actions: list[CleanAction] = []
    snap_before = _snapshot_dict() if before is None else before
    before_used = _before_used_value(snap_before)

    processes = ntapi.enum_processes()
    foreground_pid = ntapi.get_foreground_pid()
    targets = pick_targets(profile, processes, foreground_pid)

    # --- trims por processo -------------------------------------------------
    for proc in targets:
        pid = proc["pid"]
        name = str(proc.get("name", "")).strip()
        label = f"{pid}:{name}"
        if dry_run:
            actions.append(CleanAction(
                kind="trim_process", target=label, status="skipped",
                ntstatus=0, detail="[dry-run] seria aparado"))
            continue
        st = ntapi.empty_working_set(pid)
        if ntapi.NT_SUCCESS(st):
            actions.append(CleanAction(
                kind="trim_process", target=label, status="ok", ntstatus=st,
                detail="working set esvaziado com sucesso"))
        else:
            # Fallback: SetProcessWorkingSetSize(-1,-1) — mesmo efeito prático.
            st2 = ntapi.trim_process(pid)
            if ntapi.NT_SUCCESS(st2):
                actions.append(CleanAction(
                    kind="trim_process", target=label, status="ok", ntstatus=st2,
                    detail="fallback trim_process (SetProcessWorkingSetSize -1,-1) aplicado"))
            else:
                actions.append(CleanAction(
                    kind="trim_process", target=label, status="failed", ntstatus=st2,
                    detail=f"EmptyWorkingSet falhou (0x{st & 0xFFFFFFFF:08X}) e "
                           f"trim_process também (0x{st2 & 0xFFFFFFFF:08X})"))

    # --- purge_standby conforme perfil --------------------------------------
    kind_purge = {"low_prio": "purge_standby_low", "full": "purge_standby_full"}.get(
        profile.purge_standby)
    if kind_purge is None:
        if not dry_run and profile.purge_standby != "none":
            actions.append(CleanAction(
                kind="purge_standby_low", target="sistema", status="skipped",
                ntstatus=0,
                detail=f"perfil '{profile.purge_standby}' inválido; nada executado"))
    elif dry_run:
        actions.append(CleanAction(
            kind=kind_purge, target="sistema", status="skipped", ntstatus=0,
            detail="[dry-run] seria executado purge da standby list"))
    else:
        # Executamos e CLASSIFICAMOS o NTSTATUS: sem elevação o kernel devolve
        # 0xC0000061 (STATUS_PRIVILEGED_INSTRUCTION) — isso é skip, não erro.
        st = ntapi.purge_standby(low_only=(profile.purge_standby == "low_prio"))
        if ntapi.NT_SUCCESS(st):
            actions.append(CleanAction(
                kind=kind_purge, target="sistema", status="ok", ntstatus=st,
                detail="purge da standby list executado"))
        elif (st & 0xFFFFFFFF) == 0xC0000061:
            actions.append(CleanAction(
                kind=kind_purge, target="sistema", status="skipped",
                ntstatus=st,
                detail="sessão sem elevação: purge retorna 0xC0000061 "
                       "(STATUS_PRIVILEGED_INSTRUCTION); execute como administrador"))
        else:
            actions.append(CleanAction(
                kind=kind_purge, target="sistema", status="failed", ntstatus=st,
                detail=f"purge da standby list falhou (0x{st & 0xFFFFFFFF:08X})"))

    # --- flush_modified conforme perfil --------------------------------------
    if not profile.flush_modified:
        pass  # perfil desliga flush: nada a registrar
    elif dry_run:
        actions.append(CleanAction(
            kind="flush_modified", target="sistema", status="skipped",
            ntstatus=0,
            detail="[dry-run] seria executado flush da modified list"))
    else:
        # ntapi.flush_modified() já devolve STATUS_SKIPPED_NOT_ELEVATED sem
        # chamar a syscall quando não elevado (evita o falso-positivo 0x0
        # validado empiricamente neste host).
        st = ntapi.flush_modified()
        if (st & 0xFFFFFFFF) == _STATUS_SKIPPED_NOT_ELEVATED:
            actions.append(CleanAction(
                kind="flush_modified", target="sistema", status="skipped",
                ntstatus=st,
                detail="sessão sem elevação: MemoryFlushModifiedList retorna "
                       "0x0 (falso-positivo) neste host; pulado por segurança"))
        elif ntapi.NT_SUCCESS(st):
            actions.append(CleanAction(
                kind="flush_modified", target="sistema", status="ok",
                ntstatus=st, detail="flush da modified list executado"))
        else:
            actions.append(CleanAction(
                kind="flush_modified", target="sistema", status="failed",
                ntstatus=st,
                detail=f"flush da modified list falhou (0x{st & 0xFFFFFFFF:08X})"))

    # --- delta contábil ------------------------------------------------------
    snap_after = _snapshot_dict()
    after_used = _used_bytes(snap_after)
    freed = before_used - after_used

    duration_ms = (time.perf_counter() - t0) * 1000.0
    return CleanReport(
        dry_run=dry_run, actions=actions,
        freed_accounting=freed, duration_ms=duration_ms)


def _hasattr(obj, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except AttributeError:
        return False


def _before_used_value(snap_before) -> int:
    """Aceita MemorySnapshot real (atributo physical_used) ou dict proxy."""
    if _hasattr(snap_before, "physical_used"):
        return int(snap_before.physical_used)
    return _used_bytes(snap_before)
