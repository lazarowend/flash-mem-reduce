# -*- coding: utf-8 -*-
"""
flashmem.core.measure — snapshot de memória real do sistema.

AGENTE B do plano Flash Mem (PLANO_IMPLEMENTACAO.md seção 3).
100% LEITURA: nenhuma syscall de escrita/limpeza aqui — apenas queries.
Toda a instrumentação nativa vem de flashmem.core.ntapi (AGENTE A, já validado):
este módulo NÃO reimplementa bindings, só compõe os resultados em um dataclass.

Unidades: TODOS os campos de memória em BYTES. ntapi.query_memory_lists()
retorna contadores em PÁGINAS; convertemos com page_size antes de expor.
"""

import ctypes
import struct
import time
from ctypes import wintypes
from dataclasses import dataclass

from flashmem.core import ntapi

__all__ = ["MemorySnapshot", "snapshot"]


@dataclass(frozen=True)
class MemorySnapshot:
    """Fotografia coesa do estado de memória em um instante (todos os valores em bytes).

    hard_faults_total é o contador PageReadCount cumulativo desde o boot
    (monotônico por definição — usado por decide.py para taxa de faults).
    """

    timestamp: float
    physical_total: int        # bytes
    physical_available: int    # AvailablePages*PageSize
    physical_used: int         # total - available
    standby_total: int         # bytes; 0 se query indisponível
    standby_low_priority: int  # bytes; 0 se indisponível
    modified_list: int
    zeroed_list: int
    free_list: int
    commit_used: int
    commit_limit: int
    commit_peak: int
    hard_faults_total: int     # PageReadCount cumulativo
    pagefile_used: int
    pagefile_limit: int


def _physical_total_bytes(page_size: int) -> int:
    """Total de memória física via SystemBasicInformation (leitura pura).

    ntapi não expõe NumberOfPhysicalPages publicamente, mas a struct que
    ntapi._page_size() lê (SYSTEM_BASIC_INFORMATION) contém o campo no
    offset 0x0C (ULONG) — mesma struct/offset já validados pelo AGENTE A
    (PageSize@0x08, NumberOfPhysicalPages@0x0C). Repetimos a mesma query
    de leitura isoladamente, reutilizando as constantes/assinaturas JÁ
    configuradas pelo AGENTE A — nenhum binding novo é criado aqui.
    """
    buf = ctypes.create_string_buffer(64)
    rlen = wintypes.ULONG(0)
    st = ntapi._ntdll.NtQuerySystemInformation(
        ntapi.SYSTEM_BASIC_INFORMATION, buf, ctypes.sizeof(buf), ctypes.byref(rlen)
    )
    if not ntapi.NT_SUCCESS(st):
        raise OSError(st & 0xFFFFFFFF, "query de NumberOfPhysicalPages falhou")
    return struct.unpack_from("<I", buf.raw, 0x0C)[0] * page_size


def snapshot() -> MemorySnapshot:
    """Coleta um MemorySnapshot (apenas queries de leitura; erros propagam).

    Fontes:
    - ntapi.query_performance(): páginas disponíveis, commit, hard faults.
    - ntapi.query_memory_lists(): listas de PFN (zero/free/modified/standby) em
      PÁGINAS — None em Windows sem suporte; snapshot segue com zeros nesses
      campos (contrato da seção 3 do plano: "0 se query indisponível").
    - ntapi.query_pagefile(): uso/limite de pagefile em bytes.

    Se qualquer query falhar com OSError, a exceção propaga — erro de syscall
    nunca é silencioso (regra 6 do plano).
    """
    perf = ntapi.query_performance()
    page_size: int = perf["page_size"]

    physical_total = _physical_total_bytes(page_size)
    physical_available = perf["available_pages"] * page_size
    physical_used = physical_total - physical_available

    lists = ntapi.query_memory_lists()
    if lists is not None:
        # listas vêm em PÁGINAS -> converte para BYTES (contrato: bytes)
        standby_total = lists["standby"] * page_size
        modified_list = lists["modified"] * page_size
        zeroed_list = lists["zero"] * page_size
        free_list = lists["free"] * page_size
    else:
        # query SystemMemoryListInformation indisponível: contrato do plano
        # manda seguir com 0 nos campos de listas (snapshot não pode falhar).
        standby_total = modified_list = zeroed_list = free_list = 0

    # standby_low_priority: a query atual (SystemMemoryListInformation, retlen
    # 176 = 22 qwords) traz contadores por prioridade de standby, mas os qwords
    # de prioridades individuais não têm semântica decodificada/validada no
    # phnt consultado — o AGENTE A validou apenas zero@0, free@8, modified@16,
    # standby@80. Sem decodificação confiável, reportamos 0 (limitação
    # documentada; a purga de baixa prioridade usa purge_standby(low_only=True)
    # independente deste valor).
    standby_low_priority = 0

    pf = ntapi.query_pagefile()

    return MemorySnapshot(
        timestamp=time.time(),
        physical_total=physical_total,
        physical_available=physical_available,
        physical_used=physical_used,
        standby_total=standby_total,
        standby_low_priority=standby_low_priority,
        modified_list=modified_list,
        zeroed_list=zeroed_list,
        free_list=free_list,
        commit_used=perf["committed_pages"] * page_size,
        commit_limit=perf["commit_limit"] * page_size,
        commit_peak=perf["peak_commit"] * page_size,
        hard_faults_total=perf["page_read_count"],
        pagefile_used=pf["used_bytes"],
        pagefile_limit=pf["limit_bytes"],
    )
