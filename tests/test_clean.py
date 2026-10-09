# -*- coding: utf-8 -*-
"""
test_clean.py — AGENTE D (Onda 2): clean.py + verify.py.

Estratégia anti-flaky: NÃO validamos working sets empiricamente (WS muda
sozinho entre amostras). Em vez disso, mocks via unittest.mock (stdlib)
registram as chamadas de mutação — o contrato é:
- dry-run: NENHUMA chamada a empty_working_set/trim_process/purge_standby/flush_modified
- execute: chamadas exatamente nos pids alvo certos.
Smoke real seguro no fim: EmptyWorkingSet no PRÓPRIO pid do teste
(operação permitida sem elevação, PLANO seção 4 oráculo (e)).
"""

import os
import unittest
from dataclasses import dataclass
from unittest import mock

from flashmem.core import clean as clean_mod
from flashmem.core import verify as verify_mod
from flashmem.core.clean import EXCLUDE_NAMES, CleanReport, clean, pick_targets
from flashmem.core.config import PROFILES
from flashmem.core.verify import EfficacyReport, verify

_MB = 1024 * 1024


def _fake_snapshot(used: int):
    """MemorySnapshot mínimo (duck-typed) para verify/clean."""
    obj = mock.MagicMock()
    obj.physical_used = used
    return obj


def _fake_processes():
    """Lista sintética: 6 candidatos grandes + excluídos + foreground + self."""
    self_pid = os.getpid()
    procs = [
        {"pid": 1000, "name": "BigApp.exe", "ws_bytes": 900 * _MB, "priority": 8},
        {"pid": 1001, "name": "MidApp.exe", "ws_bytes": 600 * _MB, "priority": 8},
        {"pid": 1002, "name": "SmallApp.exe", "ws_bytes": 150 * _MB, "priority": 8},
        {"pid": 1003, "name": "svchost.exe", "ws_bytes": 800 * _MB, "priority": 8},
        {"pid": 1004, "name": "explorer.exe", "ws_bytes": 700 * _MB, "priority": 8},
        {"pid": 1005, "name": "TinyApp.exe", "ws_bytes": 10 * _MB, "priority": 8},
        {"pid": 1006, "name": "Python.EXE", "ws_bytes": 500 * _MB, "priority": 8},
        {"pid": self_pid, "name": "python.exe", "ws_bytes": 400 * _MB, "priority": 8},
        {"pid": 1007, "name": "FGApp.exe", "ws_bytes": 999 * _MB, "priority": 8},
    ]
    return procs


class TestDryRunNeverMutates(unittest.TestCase):
    """(i) dry-run NUNCA chama nenhuma syscall de mutação."""

    def test_dry_run_calls_nothing(self):
        profile = PROFILES["aggressive"]  # purge full + flush: tudo seria chamado
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=_fake_processes()), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=1007), \
             mock.patch.object(clean_mod.ntapi, "empty_working_set",
                               return_value=0) as m_ews, \
             mock.patch.object(clean_mod.ntapi, "trim_process",
                               return_value=0) as m_trim, \
             mock.patch.object(clean_mod.ntapi, "purge_standby",
                               return_value=0) as m_purge, \
             mock.patch.object(clean_mod.ntapi, "flush_modified",
                               return_value=0) as m_flush, \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(profile, dry_run=True)
        self.assertIsInstance(rep, CleanReport)
        self.assertTrue(rep.dry_run)
        m_ews.assert_not_called()
        m_trim.assert_not_called()
        m_purge.assert_not_called()
        m_flush.assert_not_called()
        # Plano presente: ações skipped com detail marcado
        trims = [a for a in rep.actions if a.kind == "trim_process"]
        self.assertGreater(len(trims), 0)
        for a in trims:
            self.assertEqual(a.status, "skipped")
            self.assertIn("dry-run", a.detail)
            self.assertEqual(a.ntstatus, 0)

    def test_execute_calls_expected_pids(self):
        """(i)+(iv) execute chama empty_working_set nos pids alvo certos."""
        profile = PROFILES["standard"]  # trim_top=5, min_ws_mb=300
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=_fake_processes()), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=1007), \
             mock.patch.object(clean_mod.ntapi, "empty_working_set",
                               return_value=0) as m_ews, \
             mock.patch.object(clean_mod.ntapi, "trim_process",
                               return_value=0) as m_trim, \
             mock.patch.object(clean_mod.ntapi, "is_elevated",
                               return_value=False), \
             mock.patch.object(clean_mod.ntapi, "purge_standby",
                               return_value=0xC0000061) as m_purge, \
             mock.patch.object(clean_mod.ntapi, "flush_modified",
                               return_value=0x40010000) as m_flush, \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(profile, dry_run=False)
        self.assertFalse(rep.dry_run)
        # Candidatos ≥300MB, não excluídos, não foreground(1007), não self:
        # 1000 (900MB), 1001 (600MB) — svchost/explorer/python excluídos, FG excluído.
        called = [c.args[0] for c in m_ews.call_args_list]
        self.assertEqual(called, [1000, 1001])
        m_trim.assert_not_called()  # emptyWorkingSet retornou sucesso
        m_purge.assert_called_once_with(low_only=True)  # standard = low_prio
        m_flush.assert_called_once_with()
        # Ações de sistema sem elevação -> skipped com motivo 0xC0000061
        purge_act = [a for a in rep.actions if a.kind == "purge_standby_low"][0]
        self.assertEqual(purge_act.status, "skipped")
        self.assertIn("0xC0000061", purge_act.detail)
        flush_act = [a for a in rep.actions if a.kind == "flush_modified"][0]
        self.assertEqual(flush_act.status, "skipped")
        self.assertIn("falso-positivo", flush_act.detail)

    def test_fallback_trim_on_failure(self):
        """Execute com EmptyWorkingSet falhando → fallback trim_process."""
        profile = PROFILES["standard"]
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=_fake_processes()), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=1007), \
             mock.patch.object(clean_mod.ntapi, "empty_working_set",
                               return_value=0xC0000022) as m_ews, \
             mock.patch.object(clean_mod.ntapi, "trim_process",
                               return_value=0) as m_trim, \
             mock.patch.object(clean_mod.ntapi, "purge_standby",
                               return_value=0xC0000061) as m_purge, \
             mock.patch.object(clean_mod.ntapi, "flush_modified",
                               return_value=0x40010000), \
             mock.patch.object(clean_mod.ntapi, "is_elevated",
                               return_value=False), \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(profile, dry_run=False)
        self.assertEqual(m_ews.call_count, 2)
        self.assertEqual(m_trim.call_count, 2)
        for a in rep.actions:
            if a.kind == "trim_process":
                self.assertEqual(a.status, "ok")
                self.assertIn("fallback", a.detail)

    def test_purge_kind_full(self):
        """Perfil aggressive usa purge_standby_full com low_only=False."""
        profile = PROFILES["aggressive"]
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=[]), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=None), \
             mock.patch.object(clean_mod.ntapi, "is_elevated",
                               return_value=True), \
             mock.patch.object(clean_mod.ntapi, "purge_standby",
                               return_value=0) as m_purge, \
             mock.patch.object(clean_mod.ntapi, "flush_modified",
                               return_value=0) as m_flush, \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(profile, dry_run=False)
        m_purge.assert_called_once_with(low_only=False)
        purge_act = [a for a in rep.actions if a.kind == "purge_standby_full"]
        self.assertEqual(len(purge_act), 1)
        self.assertEqual(purge_act[0].status, "ok")
        m_flush.assert_called_once_with()

    def test_purge_none_profile(self):
        """Perfil safe (purge none, flush False) não registra ações de sistema."""
        profile = PROFILES["safe"]
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=[]), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=None), \
             mock.patch.object(clean_mod.ntapi, "purge_standby",
                               return_value=0) as m_purge, \
             mock.patch.object(clean_mod.ntapi, "flush_modified",
                               return_value=0) as m_flush, \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(profile, dry_run=False)
        m_purge.assert_not_called()
        m_flush.assert_not_called()
        self.assertEqual(rep.actions, [])


class TestExclusions(unittest.TestCase):
    """(ii) EXCLUDE_NAMES nunca é alvo; (iii) foreground nunca é aparado."""

    def test_excluded_never_target(self):
        procs = _fake_processes()
        for pid in (1003, 1004, 1006, os.getpid()):  # svchost, explorer, Python.EXE, self
            self.assertNotIn(pid, [p["pid"] for p in pick_targets(PROFILES["aggressive"], procs, 1007)])
        # EXCLUDE_NAMES casa em lowercase (Python.EXE → python.exe)
        self.assertIn("python.exe", EXCLUDE_NAMES)

    def test_foreground_never_trimmed(self):
        """(iii) foreground = pid do MAIOR WS; garante que ele fica fora."""
        procs = _fake_processes()  # maior WS = 1007 FGApp.exe (999MB)
        targets = pick_targets(PROFILES["aggressive"], procs, foreground_pid=1007)
        self.assertEqual([p["pid"] for p in targets], [1000, 1001, 1002])

    def test_case_insensitive_name_match(self):
        """Nome com maiúsculas (Python.EXE) é excluído via lowercase."""
        procs = [{"pid": 2000, "name": "LSASS.EXE", "ws_bytes": 2 * 1024**3, "priority": 9}]
        self.assertEqual(pick_targets(PROFILES["aggressive"], procs, None), [])


class TestTopN(unittest.TestCase):
    """(iv) Só os trim_top maiores passam do filtro."""

    def test_topn_limits_count(self):
        # 8 processos válidos de 800..100MB; aggressive: top=10 → todos (8 < 10);
        # standard: top=5, min 300MB → 5 maiores ≥300MB.
        procs = [
            {"pid": i, "name": f"App{i}.exe", "ws_bytes": (900 - i * 100) * _MB, "priority": 8}
            for i in range(1, 9)  # 800..100 MB
        ]
        t_agg = pick_targets(PROFILES["aggressive"], procs, None)
        self.assertEqual(len(t_agg), 8)
        t_std = pick_targets(PROFILES["standard"], procs, None)
        self.assertEqual([p["pid"] for p in t_std], [1, 2, 3, 4, 5, 6, 7][:5])
        # Ordenação desc por WS
        ws = [p["ws_bytes"] for p in t_agg]
        self.assertEqual(ws, sorted(ws, reverse=True))

    def test_min_ws_threshold(self):
        """Abaixo de min_ws_mb não é candidato."""
        procs = [{"pid": 1, "name": "A.exe", "ws_bytes": 299 * _MB, "priority": 8},
                 {"pid": 2, "name": "B.exe", "ws_bytes": 300 * _MB, "priority": 8}]
        # standard min_ws_mb=300: 299MB fora (limite inclusivo >= 300MB)
        self.assertEqual([p["pid"] for p in pick_targets(PROFILES["standard"], procs, None)], [2])


class TestVerify(unittest.TestCase):
    """(v) verify() nas 3 situações do note."""

    def test_real_relief_when_faults_drop(self):
        rep = verify(_fake_snapshot(10 * _MB), _fake_snapshot(5 * _MB),
                     faults_rate_before=8.0, faults_rate_after=3.0)
        self.assertIsInstance(rep, EfficacyReport)
        self.assertEqual(rep.freed_accounting, 5 * _MB)
        self.assertEqual(rep.note, "alívio real provável")

    def test_accounting_only_when_faults_rise_or_flat(self):
        # faults constantes
        rep = verify(_fake_snapshot(10 * _MB), _fake_snapshot(4 * _MB), 5.0, 5.0)
        self.assertIn("ganho apenas contábil", rep.note)
        self.assertIn("standby", rep.note)
        # faults subiram
        rep2 = verify(_fake_snapshot(10 * _MB), _fake_snapshot(4 * _MB), 3.0, 9.0)
        self.assertIn("ganho apenas contábil", rep2.note)
        self.assertEqual(rep2.freed_accounting, 6 * _MB)

    def test_no_evidence_when_no_gain(self):
        # freed == 0 e faults constantes
        rep = verify(_fake_snapshot(10 * _MB), _fake_snapshot(10 * _MB), 5.0, 5.0)
        self.assertEqual(rep.note, "sem evidência de alívio")
        # freed negativo (piorou) e faults constantes
        rep2 = verify(_fake_snapshot(5 * _MB), _fake_snapshot(8 * _MB), 5.0, 5.0)
        self.assertEqual(rep2.freed_accounting, -3 * _MB)
        self.assertEqual(rep2.note, "sem evidência de alívio")

    def test_real_relief_even_without_accounting_gain(self):
        """Faults caíram com freed<=0: ainda é alívio real (regra 1 prevalece)."""
        rep = verify(_fake_snapshot(5 * _MB), _fake_snapshot(8 * _MB), 9.0, 2.0)
        self.assertEqual(rep.note, "alívio real provável")
        self.assertEqual(rep.freed_accounting, -3 * _MB)


class TestFreedAccounting(unittest.TestCase):
    """freed_accounting preserva valores reais, inclusive NEGATIVOS."""

    def test_negative_freed_preserved(self):
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=[]), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=None), \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               side_effect=[{"physical_used": 100},
                                            {"physical_used": 250}]):
            rep = clean(PROFILES["safe"], dry_run=True)
        self.assertEqual(rep.freed_accounting, 100 - 250)
        self.assertLess(rep.freed_accounting, 0)

    def test_duration_ms_positive(self):
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=[]), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=None), \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               return_value={"physical_used": 1}):
            rep = clean(PROFILES["safe"], dry_run=True)
        self.assertGreaterEqual(rep.duration_ms, 0.0)

    def test_before_object_used_directly(self):
        """before passado como MemorySnapshot real não refaz snapshot."""
        before_snap = _fake_snapshot(700)
        with mock.patch.object(clean_mod.ntapi, "enum_processes",
                               return_value=[]), \
             mock.patch.object(clean_mod.ntapi, "get_foreground_pid",
                               return_value=None), \
             mock.patch.object(clean_mod, "_snapshot_dict",
                               side_effect=[{"physical_used": 700}]) as m_snap:
            rep = clean(PROFILES["safe"], dry_run=True, before=before_snap)
        # _snapshot_dict chamado 1x (só 'after') — before veio do chamador
        self.assertEqual(m_snap.call_count, 1)
        self.assertEqual(rep.freed_accounting, 0)


class TestRealSmoke(unittest.TestCase):
    """(vi) Smoke REAL seguro: trim apenas do PRÓPRIO processo de teste.

    EmptyWorkingSet no próprio pid é permitido sem elevação (oráculo (e)
    do PLANO seção 4). Não tocamos em nenhum outro processo.
    """

    def test_trim_self_pid_real(self):
        self_pid = os.getpid()
        st = clean_mod.ntapi.empty_working_set(self_pid)
        self.assertTrue(clean_mod.ntapi.NT_SUCCESS(st),
                        msg=f"EmptyWorkingSet(próprio pid) falhou: 0x{st & 0xFFFFFFFF:08X}")

    def test_clean_dry_run_default_is_true(self):
        """Contrato: assinatura de clean() tem dry_run=True como default."""
        import inspect
        sig = inspect.signature(clean)
        self.assertIs(sig.parameters["dry_run"].default, True)


if __name__ == "__main__":
    unittest.main()
