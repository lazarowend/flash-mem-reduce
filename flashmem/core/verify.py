# -*- coding: utf-8 -*-
"""
flashmem.core.verify — verificação de eficácia pós-limpeza (AGENTE D, Onda 2).

A pergunta central do projeto: a limpeza aliviou a pressão REAL de memória,
ou apenas moveu páginas do working set para a standby list (continuando na
RAM e podendo voltar com hard faults)?

Dois sinais independentes:
1. freed_accounting — delta CONTÁBIL do físico usado (antes - depois).
   Pode ser negativo; não é prova de alívio por si só.
2. Taxa de hard faults (faults/s) antes vs depois — prova empírica de
   pressão real (leitura de disco para repor páginas).

Regra do note (PLANO_IMPLEMENTACAO.md seção 3, verify.py):
- taxa de faults CAIU  -> "alívio real provável"
- freed_accounting > 0 mas faults subiram/constantes -> "ganho apenas contábil
  (páginas migraram para standby; continuam na RAM)"
- demais casos -> "sem evidência de alívio"
"""

import math
from dataclasses import dataclass

__all__ = ["EfficacyReport", "verify"]

# Tolerância numérica: taxas iguais a menos de 1e-9 são "constantes".
_EPS = 1e-9


@dataclass
class EfficacyReport:
    """Veredicto pós-limpeza."""
    freed_accounting: int  # bytes; delta contábil (pode ser negativo)
    note: str              # pt-BR


def _used_bytes(snap) -> int:
    """physical_used de um MemorySnapshot (ou dict com a mesma chave)."""
    if hasattr(snap, "physical_used"):
        return int(snap.physical_used)
    if isinstance(snap, dict):
        return int(snap.get("physical_used", 0))
    raise TypeError(f"snapshot sem campo physical_used: {type(snap)!r}")


def verify(before, after,
           faults_rate_before: float, faults_rate_after: float) -> EfficacyReport:
    """Compara snapshots antes/depois + taxas de hard faults e devolve o veredicto."""
    freed_accounting = _used_bytes(before) - _used_bytes(after)

    fb = float(faults_rate_before)
    fa = float(faults_rate_after)
    if math.isnan(fb) or math.isnan(fa):
        note = "sem evidência de alívio"
    elif fa < fb - _EPS:
        # Taxa de hard faults caiu: menos leituras de disco para repor páginas.
        note = "alívio real provável"
    elif freed_accounting > 0 and fa >= fb - _EPS:
        # Ganho contábil sem queda de faults: páginas foram para standby.
        note = ("ganho apenas contábil (páginas migraram para standby; "
                "continuam na RAM)")
    else:
        # freed_accounting <= 0 e faults não caíram: nada a celebrar.
        note = "sem evidência de alívio"

    return EfficacyReport(freed_accounting=freed_accounting, note=note)
