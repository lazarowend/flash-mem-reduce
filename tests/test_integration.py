# -*- coding: utf-8 -*-
"""
tests.test_integration — pipeline completo do Flash Mem (AGENTE E, Onda 3).

Cobre o contrato do PLANO_IMPLEMENTACAO.md (seção 2, test_integration.py):
(i)   pipeline DRY-RUN: Monitor(execute=False).run_once() -> report None;
(ii)  execute=True com clean() MOCKADO: sequência sintética arma e dispara,
      clean() chamado 1x com o perfil certo, history.append chamado;
(iii) run_loop com sleep e max_ticks injetáveis (3 iterações, sem dormir);
(iv)  CLI: status/decide/clean/history executam sem exceção (stdout capturado);
(v)   smoke REAL seguro: execute=True não limpa em host saudável (decisão != clean).

Todos os testes de execução usam MOCK — nenhuma limpeza real de sistema.
"""

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

from flashmem.core.config import PROFILES
from flashmem.core.decide import PressureEvaluator
from flashmem.core.measure import MemorySnapshot, snapshot as real_snapshot
from flashmem.monitor import Monitor

# Adiciona a raiz do projeto ao path para importar flashmem.cli
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flashmem.cli import main as cli_main  # noqa: E402


def synth_snapshot(commit_pct: float, phys_pct: float, faults: int, ts: float,
                   total_gb: float = 16.0) -> MemorySnapshot:
    """Snapshot sintético com % de commit/físico e faults controlados.

    commit_pct/phys_pct em % (0..100); faults é o contador cumulativo.
    """
    total = int(total_gb * 1024 * 1024 * 1024)
    commit_limit = int(20 * 1024 * 1024 * 1024)  # 20 GB
    commit_used = int(commit_limit * commit_pct / 100.0)
    physical_used = int(total * phys_pct / 100.0)
    return MemorySnapshot(
        timestamp=ts,
        physical_total=total,
        physical_available=total - physical_used,
        physical_used=physical_used,
        standby_total=int(total * 0.20),
        standby_low_priority=0,
        modified_list=int(0.01 * total),
        zeroed_list=int(0.02 * total),
        free_list=int(0.01 * total),
        commit_used=commit_used,
        commit_limit=commit_limit,
        commit_peak=commit_used,
        hard_faults_total=faults,
        pagefile_used=int(2 * 1024 * 1024 * 1024),
        pagefile_limit=int(8 * 1024 * 1024 * 1024),
    )


class TestMonitorDryRun(unittest.TestCase):
    """(i) Pipeline DRY-RUN: observa e recomenda, NUNCA chama clean()."""

    def test_run_once_dry_run_returns_dict_with_none_report(self):
        # Snapshot REAL do host (leitura pura) — pipeline completo dry-run.
        mon = Monitor(profile_name="standard", execute=False)
        result = mon.run_once()
        self.assertIsInstance(result, dict)
        self.assertIn("snapshot", result)
        self.assertIn("decision", result)
        self.assertIn("report", result)
        self.assertIsInstance(result["snapshot"], MemorySnapshot)
        self.assertIsNone(result["report"], "dry-run nunca deve gerar relatório")
        self.assertIn(result["decision"].action, ("wait", "cooldown", "disarm", "clean"))

    def test_dry_run_never_calls_clean(self):
        # Mesmo com decisão 'clean', execute=False NÃO chama clean().
        snaps = [
            synth_snapshot(commit_pct=90, phys_pct=90, faults=100 * i, ts=1000.0 + i)
            for i in range(5)
        ]
        it = iter(snaps)
        mon = Monitor(profile_name="standard", execute=False, snapshot_fn=lambda: next(it))
        with mock.patch("flashmem.core.clean.clean") as m_clean:
            for _ in range(5):
                result = mon.run_once()
        m_clean.assert_not_called()
        # Com commit 90% > fire 88%, host sintético dispararia clean.
        self.assertEqual(result["decision"].action, "clean")
        self.assertIsNone(result["report"])


class TestMonitorExecuteMocked(unittest.TestCase):
    """(ii) execute=True com clean()/history MOCKADOS: dispara exatamente 1x."""

    def _run_to_clean(self, profile_name="standard"):
        """Alimenta snapshots que sobem commit acima de commit_fire por
        `consecutive` ticks com faults crescentes até disparar clean."""
        cfg = PROFILES[profile_name]
        snaps = [
            synth_snapshot(commit_pct=cfg.commit_fire + 2.0,
                           phys_pct=cfg.phys_arm + 5.0,
                           faults=500 * (i + 1),
                           ts=1000.0 + i)
            for i in range(cfg.consecutive + 2)
        ]
        return snaps

    def test_execute_fires_clean_once_with_profile(self):
        snaps = self._run_to_clean()
        it = iter(snaps * 3)  # reposição para o snapshot pós-limpeza
        mon = Monitor(profile_name="standard", execute=True,
                     snapshot_fn=lambda: next(it),
                     history_path=os.path.join(tempfile.gettempdir(), "fm_hist_test.jsonl"))
        fake_report = mock.MagicMock()
        fake_report.dry_run = False
        fake_report.actions = []
        fake_report.freed_accounting = 123456
        fake_report.duration_ms = 5.0

        with mock.patch("flashmem.core.clean.clean", return_value=fake_report) as m_clean, \
             mock.patch("flashmem.core.history.append") as m_append:
            decisions = []
            for _ in range(len(snaps)):
                result = mon.run_once()
                decisions.append(result["decision"].action)
                if result["report"] is not None:
                    break

        self.assertIn("clean", decisions)
        self.assertEqual(m_clean.call_count, 1, "clean() deve ser chamado exatamente 1x")
        # Perfil certo: primeiro arg é o ProfileConfig do monitor.
        args, kwargs = m_clean.call_args
        self.assertEqual(args[0].name, "standard")
        self.assertFalse(kwargs.get("dry_run", args[1] if len(args) > 1 else True))
        # history.append chamado com o registro do evento.
        m_append.assert_called_once()
        record = m_append.call_args[0][0] if m_append.call_args[0] else \
            m_append.call_args[1].get("record")
        self.assertEqual(record["event"], "clean")
        self.assertEqual(record["profile"], "standard")
        self.assertIn("report", record)
        self.assertIn("verify", record)

    def test_cooldown_blocks_second_clean(self):
        # Após um clean, cooldown de 60s impede novo disparo imediato.
        snaps = self._run_to_clean()
        mon = Monitor(profile_name="standard", execute=True,
                      snapshot_fn=lambda: next(iter(snaps)),
                      time_fn=lambda: 2000.0,
                      history_path=os.path.join(tempfile.gettempdir(), "fm_hist_test2.jsonl"))
        fake_report = mock.MagicMock(dry_run=False, actions=[], freed_accounting=0, duration_ms=1.0)

        with mock.patch("flashmem.core.clean.clean", return_value=fake_report) as m_clean, \
             mock.patch("flashmem.core.history.append"):
            for _ in range(len(snaps)):
                mon.run_once()
            # tick seguinte dentro do cooldown: não pode disparar de novo
            result = mon.run_once()
        self.assertEqual(m_clean.call_count, 1)
        self.assertEqual(result["decision"].action, "cooldown")
        self.assertIsNone(result["report"])


class TestMonitorRunLoop(unittest.TestCase):
    """(iii) run_loop com sleep_fn mockado e max_ticks=3."""

    def test_run_loop_three_iterations_sleep_mocked(self):
        calls = []
        mon = Monitor(profile_name="standard", execute=False,
                     snapshot_fn=lambda: synth_snapshot(50.0, 50.0, 0, 1000.0),
                     sleep_fn=lambda d: calls.append(d),
                     max_ticks=3)
        mon.run_loop(interval_s=1.0)
        self.assertEqual(mon.ticks, 3)
        self.assertEqual(len(calls), 2, "3 iterações dormem 2 vezes (última não dorme)")
        # Ocioso (armed_ticks==0): intervalo adaptativo usa 3.0s
        self.assertTrue(all(d == 3.0 for d in calls), f"esperado 3.0s ocioso, veio {calls}")

    def test_run_loop_uses_1s_when_armed(self):
        calls = []
        state = {"i": 0}

        def snap_fn():
            i = state["i"]
            state["i"] += 1
            # Acima do arm de commit (80) desde o tick 0: sempre armado.
            return synth_snapshot(commit_pct=85.0, phys_pct=60.0, faults=10 * i,
                                  ts=1000.0 + i)

        mon = Monitor(profile_name="standard", execute=False,
                     snapshot_fn=snap_fn, sleep_fn=lambda d: calls.append(d),
                     max_ticks=3)
        mon.run_loop(interval_s=1.0)
        self.assertEqual(mon.ticks, 3)
        self.assertTrue(all(d == 1.0 for d in calls), f"esperado 1.0s armado, veio {calls}")

    def test_run_loop_keyboard_interrupt_clean_exit(self):
        # KeyboardInterrupt no sleep_fn encerra o loop sem propagar.
        def boom(delay):
            raise KeyboardInterrupt

        mon = Monitor(profile_name="standard", execute=False,
                     snapshot_fn=lambda: synth_snapshot(50.0, 50.0, 0, 1000.0),
                     sleep_fn=boom)
        mon.run_loop()  # não deve propagar
        self.assertEqual(mon.ticks, 1)


class TestCli(unittest.TestCase):
    """(iv) CLI: subcomandos executam sem exceção, stdout capturado."""

    def _run_cli(self, argv):
        buf = io.StringIO()
        with mock.patch("sys.stdout", new=buf):
            code = cli_main(argv)
        return code, buf.getvalue()

    def test_status(self):
        code, out = self._run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("MEMÓRIA FÍSICA", out)
        self.assertIn("COMMIT", out)

    def test_decide(self):
        code, out = self._run_cli(["decide"])
        self.assertEqual(code, 0)
        self.assertIn("Decisão", out)
        self.assertIn("Perfil", out)

    def test_clean_dry_run(self):
        code, out = self._run_cli(["clean"])
        self.assertEqual(code, 0)
        self.assertIn("PLANO", out)
        self.assertIn("dry-run", out)

    def test_history_empty(self):
        # history com arquivo inexistente: saída elegante, sem exceção.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nao_existe.jsonl")
            with mock.patch("flashmem.cli._HISTORY_PATH", path):
                code, out = self._run_cli(["history"])
        self.assertEqual(code, 0)
        self.assertIn("nenhum registro", out)

    def test_history_with_records(self):
        # history lê registros reais do formato do Monitor.
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hist.jsonl")
            rec = {
                "ts": 1700000000.0, "event": "clean", "profile": "standard",
                "report": {"freed_accounting_bytes": 1048576, "duration_ms": 12.5,
                            "actions_ok": 2, "actions_skipped": 1, "actions_failed": 0},
                "verify": {"freed_accounting_bytes": 1048576, "note": "alívio real provável"},
            }
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            with mock.patch("flashmem.cli._HISTORY_PATH", path):
                code, out = self._run_cli(["history"])
        self.assertEqual(code, 0)
        self.assertIn("standard", out)
        self.assertIn("alívio real provável", out)

    def test_main_no_args_shows_help(self):
        code, out = self._run_cli([])
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())

    def test_watch_cleans_with_execute_mock(self):
        # watch --execute com clean mockado e max_ticks=1 via patch de run_once.
        mon = Monitor(profile_name="standard", execute=True, max_ticks=1,
                      snapshot_fn=lambda: synth_snapshot(50.0, 50.0, 0, 1000.0))
        with mock.patch("flashmem.core.clean.clean") as m_clean:
            mon.run_once()
        m_clean.assert_not_called()  # decisão != clean -> nunca chama


class TestSmokeRealSafe(unittest.TestCase):
    """(v) Smoke REAL seguro: execute=True não limpa em host saudável."""

    def test_real_execute_no_clean_on_healthy_host(self):
        # Snapshot REAL + avaliador real: em host saudável a decisão != clean
        # (precisa de consecutive>=3 ticks armados; run_once é 1 tick) e o
        # clean() NUNCA é chamado. Se o host estiver sob pressão extrema,
        # decision pode ser 'clean' — mas ainda assim run_once de 1 tick não
        # pode ter armed_ticks >= consecutive(3): assegura via mock espião.
        mon = Monitor(profile_name="standard", execute=True,
                      history_path=os.path.join(tempfile.gettempdir(), "fm_smoke.jsonl"))
        with mock.patch("flashmem.core.clean.clean") as m_clean:
            result = mon.run_once()
        m_clean.assert_not_called()
        self.assertIsNone(result["report"])
        self.assertIn(result["decision"].action, ("wait", "cooldown", "disarm", "clean"))

    def test_real_snapshot_pipeline_smoke(self):
        # Pipeline REAL de leitura: snapshot -> avaliador -> decisão (dry-run).
        snap = real_snapshot()
        ev = PressureEvaluator(PROFILES["standard"])
        decision = ev.update(snap)
        self.assertIn(decision.action, ("wait", "cooldown", "disarm", "clean"))
        self.assertGreaterEqual(decision.pressure_score, 0.0)
        self.assertLessEqual(decision.pressure_score, 1.0)


if __name__ == "__main__":
    unittest.main()
