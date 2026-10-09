# test_decide.py — testes DETERMINÍSTICOS da lógica de decisão (AGENTE C)
# Snapshots sintéticos construídos direto na dataclass; now_fn fake p/ cooldown.
# Execução: a partir de C:/Users/BAN/flashmem -> python -m unittest tests.test_decide -v

import json
import os
import tempfile
import unittest
from dataclasses import dataclass, replace

from flashmem.core.config import (
    PROFILES,
    ProfileConfig,
    load_config,
    save_config,
)
from flashmem.core.decide import Decision, MemorySnapshot, PressureEvaluator

# Memória sintética: 16 GiB físico, 20 GiB de limite de commit.
_GB = 1024 ** 3
PHYS_TOTAL = 16 * _GB
COMMIT_LIMIT = 20 * _GB

# Relógio fake: avançável manualmente (determinismo total do cooldown).
class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_snapshot(
    timestamp: float = 0.0,
    commit_pct: float = 50.0,
    phys_pct: float = 50.0,
    hard_faults_total: int = 1_000_000,
) -> MemorySnapshot:
    """Constrói MemorySnapshot sintético a partir de percentagens simples."""
    commit_used = int(COMMIT_LIMIT * commit_pct / 100.0)
    physical_used = int(PHYS_TOTAL * phys_pct / 100.0)
    return MemorySnapshot(
        timestamp=timestamp,
        physical_total=PHYS_TOTAL,
        physical_available=PHYS_TOTAL - physical_used,
        physical_used=physical_used,
        standby_total=4 * _GB,
        standby_low_priority=1 * _GB,
        modified_list=64 * 1024 * 1024,
        zeroed_list=32 * 1024 * 1024,
        free_list=128 * 1024 * 1024,
        commit_used=commit_used,
        commit_limit=COMMIT_LIMIT,
        commit_peak=max(commit_used, 10 * _GB),
        hard_faults_total=hard_faults_total,
        pagefile_used=4 * _GB,
        pagefile_limit=8 * _GB,
    )


class BaseDecideTest(unittest.TestCase):
    """Infra comum: avaliador com perfil standard e relógio fake."""

    def setUp(self):
        self.clock = FakeClock(start=1000.0)
        self.cfg = PROFILES["standard"]
        self.ev = PressureEvaluator(self.cfg, now_fn=self.clock)

    def feed(self, *snaps) -> list:
        return [self.ev.update(s) for s in snaps]


class TestArmingAndFiring(BaseDecideTest):
    """Armar, esperar ticks consecutivos e disparar com prova de commit."""

    def test_first_tick_below_arm_disarms(self):
        # commit 50% < 80% e físico 50% < 85%: nunca armou -> disarm.
        d = self.ev.update(make_snapshot(commit_pct=50.0, phys_pct=50.0))
        self.assertEqual(d.action, "disarm")
        self.assertEqual(d.armed_ticks, 0)
        self.assertIn("abaixo do arm", d.reason)

    def test_commit_above_fire_fires_immediately_after_consecutive(self):
        # commit 90% >= fire 88% arma E é prova; precisa de 3 ticks.
        snap = make_snapshot(commit_pct=90.0)
        d1 = self.ev.update(snap)
        d2 = self.ev.update(snap)
        d3 = self.ev.update(snap)
        self.assertEqual((d1.action, d2.action), ("wait", "wait"))
        self.assertEqual(d3.action, "clean")
        self.assertEqual(d3.armed_ticks, 3)
        self.assertIn("PROVA", d3.reason)

    def test_firing_requires_consecutive_ticks(self):
        # Armado em 2 ticks -> dispara; se cair antes, o contador zera.
        snap = make_snapshot(commit_pct=90.0)
        self.assertEqual(self.ev.update(snap).action, "wait")
        self.assertEqual(self.ev.update(snap).action, "wait")
        # Um tick abaixo do arm zera o contador de armados.
        self.assertEqual(self.ev.update(make_snapshot(commit_pct=50.0)).action, "disarm")
        # Rearma: 2 ticks -> wait, 3º -> clean (contagem recomeçou do zero).
        self.assertEqual(self.ev.update(snap).action, "wait")
        self.assertEqual(self.ev.update(snap).action, "wait")
        self.assertEqual(self.ev.update(snap).action, "clean")

    def test_armed_without_evidence_waits(self):
        # commit 83% (arm 80 <= x < fire 88), físico baixo, faults baixos:
        # armado, mas SEM prova => wait para sempre, nunca clean.
        for _ in range(10):
            d = self.ev.update(make_snapshot(commit_pct=83.0, phys_pct=60.0))
            self.assertEqual(d.action, "wait")
        self.assertEqual(d.armed_ticks, 10)

    def test_armed_by_phys_only_never_fires(self):
        # REGRA CENTRAL: físico 95% >= arm 85% arma, mas commit 60% < fire 88
        # e faults baixos => NUNCA dispara, apenas "wait".
        for i in range(12):
            d = self.ev.update(
                make_snapshot(
                    timestamp=float(i),
                    commit_pct=60.0,
                    phys_pct=95.0,
                    hard_faults_total=1_000_000 + 2 * i,  # ~2 faults/s
                )
            )
            self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.armed_ticks, self.cfg.consecutive)


class TestHardFaultsEvidence(BaseDecideTest):
    """Faults crescentes acima do limite são prova válida de pressão."""

    def _fault_snap(self, ts: float, total: int, commit_pct: float = 60.0) -> MemorySnapshot:
        # commit 60% < fire 88%: a ÚNICA prova possível aqui é faults.
        # físico 90% >= arm 85% ARMA via físico (que sozinho nunca dispara).
        return make_snapshot(
            timestamp=ts, commit_pct=commit_pct, phys_pct=90.0, hard_faults_total=total
        )

    def test_rising_faults_fire(self):
        # Armado por físico (90%), commit 60% < fire 88%: a prova é faults.
        # REFINAMENTO DO GATE (sustentação): a prova de faults precisa durar
        # `consecutive` amostras ACIMA do limite — igual à disciplina do arm.
        # Deltas: +6, +7, +8, +9 (taxas 6,7,8,9/s todas >= 4/s, crescentes).
        # => clean no 4º tick: arm 3/3 e prova sustentada 3/3 (ticks 2,3,4).
        seq = [
            self._fault_snap(0.0, 1_000_000),
            self._fault_snap(1.0, 1_000_006),  # delta 6  (prova 1/3)
            self._fault_snap(2.0, 1_000_013),  # delta 7  (prova 2/3)
            self._fault_snap(3.0, 1_000_021),  # delta 8  (prova 3/3) -> clean
            self._fault_snap(4.0, 1_000_030),  # delta 9
        ]
        decisions = self.feed(*seq)
        self.assertEqual(
            [d.action for d in decisions], ["wait", "wait", "wait", "clean", "clean"]
        )
        self.assertIn("hard faults", decisions[-1].reason)
        self.assertIn("sustentado", decisions[-1].reason)

    def test_single_fault_burst_does_not_fire(self):
        # REFINAMENTO DO GATE: uma RAJADA ISOLADA de faults (1 amostra acima
        # do limite, depois normal) NÃO dispara mais. Antes do refinamento
        # isso disparava — comportamento ajustado a pedido (gate sustentado).
        seq = [
            self._fault_snap(0.0, 1_000_000),
            self._fault_snap(1.0, 1_000_006),  # delta 6 >= 4 (prova 1/3)
            self._fault_snap(2.0, 1_000_007),  # delta 1 < 4  (prova zerada)
            self._fault_snap(3.0, 1_000_008),  # delta 1 < 4
            self._fault_snap(4.0, 1_000_009),  # delta 1 < 4
        ]
        decisions = self.feed(*seq)
        self.assertNotIn("clean", [d.action for d in decisions])

    def test_flat_high_faults_fire_equality_tolerance(self):
        # CONTRATO: crescente = cada delta >= anterior (tolerância de
        # igualdade). Deltas +6,+6,+6 (planos) contam como crescente:
        # taxa 6/s >= 4/s sustentada É pressão real (thrashing) => clean.
        seq = [self._fault_snap(float(t), 1_000_000 + 6 * t) for t in range(4)]
        decisions = self.feed(*seq)
        self.assertEqual(decisions[-1].action, "clean")

    def test_flat_low_faults_do_not_fire(self):
        # Deltas planos mas ABAIXO do limite (+1/s < 4/s): sem prova.
        seq = [self._fault_snap(float(t), 1_000_000 + 1 * t) for t in range(6)]
        for d in self.feed(*seq):
            self.assertNotEqual(d.action, "clean")

    def test_rising_but_below_limit_faults_do_not_fire(self):
        # Deltas: +2, +3, +3 (não-decrescente) mas taxa 3/s < 4/s => sem prova.
        totals = [1_000_000, 1_000_002, 1_000_005, 1_000_008]
        seq = [self._fault_snap(float(i), t) for i, t in enumerate(totals)]
        for d in self.feed(*seq):
            self.assertNotEqual(d.action, "clean")

    def test_falling_faults_do_not_fire(self):
        # Deltas: +8, +5, +3 (taxa 3/s no fim, caindo) => sem prova.
        totals = [1_000_000, 1_000_008, 1_000_013, 1_000_016]
        seq = [self._fault_snap(float(i), t) for i, t in enumerate(totals)]
        for d in self.feed(*seq):
            self.assertNotEqual(d.action, "clean")


class TestCooldown(BaseDecideTest):
    """note_clean inicia cooldown; nada dispara antes de cooldown_s."""

    def _fire_and_note_clean(self):
        snap = make_snapshot(commit_pct=90.0)
        for _ in range(3):
            d = self.ev.update(snap)
        self.assertEqual(d.action, "clean")
        self.ev.note_clean(self.clock.now)
        return d

    def test_cooldown_blocks_new_clean(self):
        self._fire_and_note_clean()
        # Mesma pressão enorme, imediatamente após a limpeza: cooldown vence.
        d = self.ev.update(make_snapshot(commit_pct=95.0))
        self.assertEqual(d.action, "cooldown")
        self.assertIn("cooldown", d.reason)

    def test_after_cooldown_fires_again(self):
        self._fire_and_note_clean()
        self.clock.advance(self.cfg.cooldown_s)  # exatamente cooldown_s
        # advance >= cooldown: _cooldown_remaining zera => disparo liberado.
        d = self.ev.update(make_snapshot(commit_pct=95.0))
        self.assertEqual(d.action, "clean")

    def test_cooldown_takes_precedence_over_evidence(self):
        self._fire_and_note_clean()
        self.clock.advance(self.cfg.cooldown_s - 1)  # falta 1s
        d = self.ev.update(make_snapshot(commit_pct=99.0))
        self.assertEqual(d.action, "cooldown")

    def test_cooldown_expires_progressively(self):
        self._fire_and_note_clean()
        self.clock.advance(10)
        self.assertEqual(self.ev.update(make_snapshot(commit_pct=95.0)).action, "cooldown")
        self.clock.advance(20)
        self.assertEqual(self.ev.update(make_snapshot(commit_pct=95.0)).action, "cooldown")
        self.clock.advance(29)
        self.assertEqual(self.ev.update(make_snapshot(commit_pct=95.0)).action, "cooldown")
        self.clock.advance(2)  # 61s no total: cooldown (60s) vencido
        self.assertEqual(self.ev.update(make_snapshot(commit_pct=95.0)).action, "clean")


class TestPressureScore(BaseDecideTest):
    """Score contínuo 0..1: monotônico, físico sozinho limitado a 0.5."""

    def test_score_range_and_monotonicity(self):
        # Score cresce monotonicamente com commit (demais vars fixas).
        pcts = [10.0, 30.0, 50.0, 70.0, 90.0, 99.0]
        scores = [
            self.ev.update(make_snapshot(commit_pct=p, phys_pct=40.0)).pressure_score
            for p in pcts
        ]
        self.assertTrue(all(0.0 <= s <= 1.0 for s in scores))
        self.assertEqual(scores, sorted(scores))

    def test_score_reaches_one_at_fire(self):
        # commit no limiar de fire (88%) => score 1.0 (prova real).
        d = self.ev.update(make_snapshot(commit_pct=88.0, phys_pct=10.0))
        self.assertAlmostEqual(d.pressure_score, 1.0, places=3)

    def test_phys_alone_capped_at_half(self):
        # Físico altíssimo, commit/faults zerados: score <= 0.5 (sem prova).
        d = self.ev.update(make_snapshot(commit_pct=10.0, phys_pct=100.0))
        self.assertLessEqual(d.pressure_score, 0.5001)

    def test_score_monotonic_in_phys(self):
        # Score também é não-decrescente em físico (com commit fixo).
        scores = [
            self.ev.update(make_snapshot(commit_pct=20.0, phys_pct=p)).pressure_score
            for p in [10.0, 40.0, 70.0, 95.0, 100.0]
        ]
        self.assertEqual(scores, sorted(scores))


class TestProfiles(unittest.TestCase):
    """PROFILES com valores EXATOS do plano + carregamento tolerante."""

    def test_profiles_exact_values(self):
        # Valores EXTRAÍDOS literalmente da seção 3 do PLANO_IMPLEMENTACAO.md.
        self.assertEqual(
            PROFILES["safe"],
            ProfileConfig(
                name="safe", commit_arm=85.0, commit_fire=92.0, phys_arm=90.0,
                consecutive=3, fault_rate_max=5.0, fault_window=3, cooldown_s=60,
                trim_top=3, min_ws_mb=500, purge_standby="none", flush_modified=False,
            ),
        )
        self.assertEqual(
            PROFILES["standard"],
            ProfileConfig(
                name="standard", commit_arm=80.0, commit_fire=88.0, phys_arm=85.0,
                consecutive=3, fault_rate_max=4.0, fault_window=3, cooldown_s=60,
                trim_top=5, min_ws_mb=300, purge_standby="low_prio", flush_modified=True,
            ),
        )
        self.assertEqual(
            PROFILES["aggressive"],
            ProfileConfig(
                name="aggressive", commit_arm=70.0, commit_fire=80.0, phys_arm=75.0,
                consecutive=2, fault_rate_max=3.0, fault_window=2, cooldown_s=45,
                trim_top=10, min_ws_mb=100, purge_standby="full", flush_modified=True,
            ),
        )

    def test_profiles_are_frozen(self):
        # dataclass frozen: tentativa de mutação lança FrozenInstanceError.
        with self.assertRaises(Exception):
            PROFILES["safe"].cooldown_s = 1  # type: ignore[misc]

    def test_aggressive_fires_earlier_than_standard(self):
        # aggressive: consecutive=2 e fire=80 => dispara 1 tick antes.
        ev = PressureEvaluator(PROFILES["aggressive"], now_fn=FakeClock())
        snap = make_snapshot(commit_pct=82.0)  # >= fire 80, < fire standard 88
        d1 = ev.update(snap)
        d2 = ev.update(snap)
        self.assertEqual((d1.action, d2.action), ("wait", "clean"))

    def test_safe_does_not_fire_at_standard_thresholds(self):
        # commit 88% arma o safe (arm 85) mas não é prova (fire 92) => wait.
        ev = PressureEvaluator(PROFILES["safe"], now_fn=FakeClock())
        for _ in range(6):
            d = ev.update(make_snapshot(commit_pct=88.0))
        self.assertEqual(d.action, "wait")


class TestConfigLoadSave(unittest.TestCase):
    """load_config: merge JSON sobre perfil base; save_config: roundtrip."""

    def _tmp_path(self, suffix: str = ".json") -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def test_missing_file_returns_standard(self):
        # Arquivo inexistente => perfil padrão "standard", sem exceção.
        missing = os.path.join(tempfile.gettempdir(), "flashmem_nao_existe_xyz.json")
        cfg = load_config(missing)
        self.assertEqual(cfg, PROFILES["standard"])

    def test_profile_field_selects_base(self):
        path = self._tmp_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"profile": "aggressive"}, fh)
        cfg = load_config(path)
        self.assertEqual(cfg, PROFILES["aggressive"])
        self.assertEqual(cfg.name, "aggressive")

    def test_user_overrides_merge_over_base(self):
        # JSON override de um campo isolado sobre a base "standard".
        path = self._tmp_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"profile": "standard", "cooldown_s": 30, "trim_top": 9}, fh)
        cfg = load_config(path)
        self.assertEqual(cfg.cooldown_s, 30)
        self.assertEqual(cfg.trim_top, 9)
        # Campos não sobrescritos permanecem da base:
        self.assertEqual(cfg.commit_fire, PROFILES["standard"].commit_fire)

    def test_invalid_json_falls_back_to_standard(self):
        path = self._tmp_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{isto não é json válido")
        cfg = load_config(path)
        self.assertEqual(cfg, PROFILES["standard"])

    def test_unknown_profile_falls_back_to_standard(self):
        path = self._tmp_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"profile": "turbo_ultra"}, fh)
        cfg = load_config(path)
        self.assertEqual(cfg, PROFILES["standard"])

    def test_save_load_roundtrip(self):
        # save -> load preserva TODOS os campos (roundtrip completo).
        path = self._tmp_path()
        original = replace(PROFILES["aggressive"], cooldown_s=37, min_ws_mb=222)
        save_config(original, path)
        loaded = load_config(path)
        self.assertEqual(loaded, original)
        self.assertEqual(loaded.name, "aggressive")

    def test_roundtrip_of_all_builtin_profiles(self):
        for profile in PROFILES.values():
            path = self._tmp_path()
            save_config(profile, path)
            self.assertEqual(load_config(path), profile)

    def test_typed_fields_survive_roundtrip(self):
        # int/float/bool/str mantêm os tipos após save+load (sem "80.0" virar str).
        path = self._tmp_path()
        original = replace(
            PROFILES["standard"],
            consecutive=4, fault_rate_max=2.5, flush_modified=False,
            purge_standby="full",
        )
        save_config(original, path)
        loaded = load_config(path)
        self.assertIsInstance(loaded.consecutive, int)
        self.assertIsInstance(loaded.fault_rate_max, float)
        self.assertIsInstance(loaded.flush_modified, bool)
        self.assertEqual(loaded, original)

    def test_bad_typed_overrides_are_ignored(self):
        # Tipos inválidos no JSON não quebram o load: campos ficam com a base.
        path = self._tmp_path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "profile": "standard",
                "cooldown_s": "sesenta",      # str não-numérica -> ignora
                "flush_modified": "sim",      # str não é bool -> ignora
                "purge_standby": "tudo",      # fora do enum -> ignora
                "consecutive": 2,             # válido -> aplica
            }, fh)
        cfg = load_config(path)
        self.assertEqual(cfg.cooldown_s, 60)
        self.assertTrue(cfg.flush_modified)
        self.assertEqual(cfg.purge_standby, "low_prio")
        self.assertEqual(cfg.consecutive, 2)


class TestDecisionContract(unittest.TestCase):
    """Contrato da Decision e robustez contra snapshots degenerados."""

    def test_decision_fields_and_types(self):
        d = Decision(
            action="wait", reason="teste", profile="standard",
            pressure_score=0.42, armed_ticks=1,
        )
        self.assertEqual(
            (d.action, d.profile, d.pressure_score, d.armed_ticks),
            ("wait", "standard", 0.42, 1),
        )

    def test_zero_limits_do_not_crash(self):
        # commit_limit=0 e physical_total=0: divisões protegidas, sem ZeroDivision.
        ev = PressureEvaluator(PROFILES["standard"], now_fn=FakeClock())
        degenerate = replace(
            make_snapshot(commit_pct=0, phys_pct=0),
            commit_limit=0,
            physical_total=0,
        )
        d = ev.update(degenerate)
        self.assertIn(d.action, ("disarm", "wait", "cooldown", "clean"))

    def test_two_evaluators_are_independent(self):
        # Estado isolado entre instâncias (sem mutable global compartilhado).
        clock = FakeClock()
        ev1 = PressureEvaluator(PROFILES["standard"], now_fn=clock)
        ev2 = PressureEvaluator(PROFILES["standard"], now_fn=clock)
        snap = make_snapshot(commit_pct=90.0)
        ev1.update(snap)
        ev1.update(snap)
        d2 = ev2.update(snap)
        self.assertEqual(d2.action, "wait")  # ev2 não herdou ticks do ev1
        self.assertEqual(d2.armed_ticks, 1)


if __name__ == "__main__":
    unittest.main()
