# -*- coding: utf-8 -*-
"""
flashmem.monitor — loop adaptativo de monitoramento (AGENTE E, Onda 3).

Pipeline por tick: measure.snapshot() → PressureEvaluator.update() →
(decisão "clean" APENAS com execute=True) → history.append + verify.

Regras do plano (seção 3, monitor.py):
- intervalo adaptativo: 1s quando armado (armed_ticks>0), 3s ocioso;
- clean() só é chamado quando execute=True E decision.action == "clean";
- sem execute: NUNCA chama clean() — apenas observa e recomenda;
- histórico + verificação gravados a cada limpeza EXECUTADA;
- Ctrl+C (KeyboardInterrupt) encerra o loop limpo.

Injeção para testes: snapshot_fn, sleep_fn, time_fn, history_path,
max_ticks (limite de iterações do run_loop; None = infinito, padrão de produção).
"""

from __future__ import annotations

import time
from typing import Callable

from flashmem.core.config import PROFILES, ProfileConfig, load_config
from flashmem.core.decide import PressureEvaluator
from flashmem.core import history
from flashmem.core.measure import snapshot as measure_snapshot

__all__ = ["Monitor"]

# Intervalos adaptativos (segundos) — plano seção 3: "1s quando armado, 3s ocioso".
INTERVAL_ARMED = 1.0
INTERVAL_IDLE = 3.0


def _summarize_report(report) -> dict:
    """Resumo serializável de um CleanReport para o registro de histórico."""
    actions = list(getattr(report, "actions", []))
    by_status: dict[str, int] = {}
    for act in actions:
        by_status[act.status] = by_status.get(act.status, 0) + 1
    failed = [a for a in actions if getattr(a, "status", "") == "failed"]
    return {
        "dry_run": bool(getattr(report, "dry_run", False)),
        "freed_accounting_bytes": int(getattr(report, "freed_accounting", 0)),
        "duration_ms": round(float(getattr(report, "duration_ms", 0.0)), 3),
        "actions_total": len(actions),
        "actions_ok": by_status.get("ok", 0),
        "actions_skipped": by_status.get("skipped", 0),
        "actions_failed": by_status.get("failed", 0),
        "failed_ntstatus_hex": [
            f"0x{int(a.ntstatus) & 0xFFFFFFFF:08X}" for a in failed
        ],
    }


class Monitor:
    """Loop de monitoramento com decisão informada e limpeza opcional.

    Uso em produção:
        mon = Monitor(profile_name="standard", execute=False)
        mon.run_loop(interval_s=1.0)   # Ctrl+C encerra

    Uso em teste (tudo injetável):
        mon = Monitor(profile_name="standard", execute=True,
                     snapshot_fn=fake_snap, sleep_fn=noop, max_ticks=3)
    """

    def __init__(
        self,
        profile_name: str = "standard",
        execute: bool = False,
        snapshot_fn: Callable[[], object] = measure_snapshot,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.time,
        history_path: str = "flashmem_history.jsonl",
        config_path: str | None = None,
        max_ticks: int | None = None,
    ):
        self.profile_name = profile_name
        self.execute = bool(execute)
        # Config: arquivo do usuário (config_path) tem precedência; senão o
        # perfil builtin puro — load_config também aceita só profile_name.
        if config_path is not None:
            self.config: ProfileConfig = load_config(config_path)
        elif profile_name in PROFILES:
            self.config = PROFILES[profile_name]
        else:
            # Nome desconhecido: cai no padrão tolerante do load_config.
            self.config = load_config()
        self.evaluator = PressureEvaluator(self.config, now_fn=time_fn)
        self.snapshot_fn = snapshot_fn
        self.sleep_fn = sleep_fn
        self.time_fn = time_fn
        self.history_path = history_path
        self.max_ticks = max_ticks
        # Estatísticas do loop (útil para CLI/tests).
        self.ticks = 0
        self.cleans = 0

    # ------------------------------------------------------------------ run_once

    def run_once(self) -> dict:
        """Um tick do pipeline. Retorna {"snapshot", "decision", "report"}.

        - report é None quando nada foi limpo (sem execute ou decisão != clean).
        - Com execute=True e decisão "clean": chama clean(), grava histórico
          (snapshot + decisão + resumo do report) e anexa o verify de eficácia
          comparando as taxas de faults antes/depois.
        - Import tardio de clean/verify: evita ciclo de import e permite ao
          unittest.mock.patch('flashmem.core.clean.clean') interceptar.
        """
        snap = self.snapshot_fn()
        decision = self.evaluator.update(snap)
        report = None

        if self.execute and decision.action == "clean":
            # Import AQUI (tardio): o patch de unittest.mock em
            # flashmem.core.clean.clean continua visível para monitor.
            from flashmem.core.clean import clean as _clean
            from flashmem.core.verify import verify as _verify

            # Taxa antes: derivada da janela do avaliador (dt<=0 → 0.0).
            faults_rate_before = self._faults_rate(snap)
            report = _clean(
                self.config, dry_run=False, before=snap)
            self.cleans += 1
            # Snapshot pós-limpeza para verificação de eficácia real.
            after = self.snapshot_fn()
            faults_rate_after = self._faults_rate(after, register=True)
            efficacy = _verify(snap, after,
                               faults_rate_before, faults_rate_after)
            # Registra o instante da limpeza (inicia cooldown no avaliador).
            self.evaluator.note_clean(self.time_fn())
            record = {
                "ts": self.time_fn(),
                "event": "clean",
                "profile": self.config.name,
                "snapshot": {
                    "physical_total": snap.physical_total,
                    "physical_used": snap.physical_used,
                    "physical_available": snap.physical_available,
                    "commit_used": snap.commit_used,
                    "commit_limit": snap.commit_limit,
                    "hard_faults_total": snap.hard_faults_total,
                },
                "decision": {
                    "action": decision.action,
                    "reason": decision.reason,
                    "pressure_score": decision.pressure_score,
                    "armed_ticks": decision.armed_ticks,
                },
                "report": _summarize_report(report),
                "verify": {
                    "freed_accounting_bytes": efficacy.freed_accounting,
                    "note": efficacy.note,
                },
                "faults_rate_before": round(faults_rate_before, 3),
                "faults_rate_after": round(faults_rate_after, 3),
            }
            history.append(record, path=self.history_path)

        return {"snapshot": snap, "decision": decision, "report": report}

    # ------------------------------------------------------------------ run_loop

    def run_loop(self, interval_s: float = 1.0) -> None:
        """Loop infinito com intervalo adaptativo; Ctrl+C encerra limpo.

        intervalo efetivo por tick:
          - armed_ticks > 0  → 1.0 s (reage rápido quando armado)
          - ocioso           → 3.0 s (economiza queries à toa)

        max_ticks (do __init__) limita as iterações — usado por testes;
        None (padrão) roda para sempre até KeyboardInterrupt.
        """
        try:
            while True:
                if self.max_ticks is not None and self.ticks >= self.max_ticks:
                    break
                result = self.run_once()
                self.ticks += 1
                decision = result["decision"]
                # Intervalo adaptativo: armado → 1s; caso contrário → 3s.
                delay = interval_s if decision.armed_ticks > 0 else INTERVAL_IDLE
                if self.max_ticks is not None and self.ticks >= self.max_ticks:
                    break  # não dorme depois da última iteração em modo teste
                self.sleep_fn(delay)
        except KeyboardInterrupt:
            # Ctrl+C: encerramento limpo — sem traceback, sem estado sujo.
            return

    # ---------------------------------------------------------------- internals

    def _faults_rate(self, snap, register: bool = False) -> float:
        """Taxa de hard faults/s entre as duas amostras mais recentes da
        janela interna do avaliador.

        - register=False (taxa ANTES): o `snap` JÁ foi registrado pelo
          update(); usa as duas últimas amostras da janela (anterior→atual).
        - register=True (taxa DEPOIS): registra o snapshot pós-limpeza na
          janela e calcula a taxa sobre ele (atual→pós).
        Primeira amostra (janela < 2) ou dt<=0 → 0.0.
        """
        window = getattr(self.evaluator, "_window", None)
        if window is not None and register:
            window.append((float(snap.timestamp), int(snap.hard_faults_total)))
        if not window or len(window) < 2:
            return 0.0
        (t0, f0), (t1, f1) = window[-2], window[-1]
        dt = t1 - t0
        return 0.0 if dt <= 0 else max(0.0, (f1 - f0) / dt)
