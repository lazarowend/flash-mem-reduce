# decide.py — lógica de decisão PURA do Flash Mem (AGENTE C, Onda 1)
#
# FILOSOFIA CENTRAL (a diferença vs. Mem Reduct): NUNCA limpar por % físico
# sozinho. Físico cheio sem commit alto e sem hard faults = cache saudável
# (standby é memória ÚTIL). Só disparamos com PROVA de pressão real:
#   (a) commit >= commit_fire, OU
#   (b) taxa de hard faults >= fault_rate_max E crescente na janela.
#
# Lógica PURA: zero syscalls, zero imports de ntapi/measure. Recebe snapshots
# (duck-typed: qualquer objeto com os campos de MemorySnapshot serve).

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Callable, Protocol

from flashmem.core.config import ProfileConfig

# ---------------------------------------------------------------- MemorySnapshot
# Import suave: na Onda 1 o measure.py do AGENTE B ainda pode não existir.
# Fallback: dataclass local 100% compatível com o contrato do plano (seção 3).
try:  # pragma: no cover - caminho coberto indiretamente pelos testes
    from flashmem.core.measure import MemorySnapshot  # type: ignore
except Exception:  # ImportError e qualquer erro de inicialização do módulo

    @dataclass(frozen=True)
    class MemorySnapshot:  # type: ignore[no-redef]
        """Contrato idêntico ao measure.py (PLANO_IMPLEMENTACAO.md, seção 3)."""

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


class _SupportsFields(Protocol):
    """Structural typing: aceita a dataclass real OU qualquer objeto compatível."""

    timestamp: float
    commit_used: int
    commit_limit: int
    physical_used: int
    physical_total: int
    hard_faults_total: int


# ---------------------------------------------------------------------- Decision


@dataclass(frozen=True)
class Decision:
    action: str            # "clean" | "wait" | "cooldown" | "disarm"
    reason: str            # pt-BR legível
    profile: str           # "safe"|"standard"|"aggressive"
    pressure_score: float  # 0.0..1.0
    armed_ticks: int


# --------------------------------------------------------------- PressureEvaluator


def _pct_commit(snap) -> float:
    """% de commit usado; 0.0 se o limite for 0/inválido (divide seguro)."""
    limit = snap.commit_limit
    if limit <= 0:
        return 0.0
    return (snap.commit_used / limit) * 100.0


def _pct_phys(snap) -> float:
    """% de físico em uso; 0.0 se o total for 0/inválido (divide seguro)."""
    total = snap.physical_total
    if total <= 0:
        return 0.0
    return (snap.physical_used / total) * 100.0


class PressureEvaluator:
    """Avalia pressão de memória tick a tick e decide: limpar ou não.

    Máquina de estados (por tick de update()):
      - ARMAR quando commit_pct >= commit_arm OU phys_pct >= phys_arm.
      - DISPARAR ("clean") quando armed_ticks >= consecutive E há PROVA:
        commit_pct >= commit_fire OU (faults/s >= fault_rate_max E taxa
        crescente nas últimas fault_window amostras).
      - phys_pct NUNCA dispara sozinho (só arma).
      - Cooldown: nada dispara antes de cooldown_s desde o último note_clean.
      - Armado sem disparo: "wait"; caiu abaixo do arm: "disarm".

    now_fn injetável => testes determinísticos de cooldown.
    """

    def __init__(self, config: ProfileConfig, now_fn: Callable[[], float] = time.time):
        self.config = config
        self.now_fn = now_fn
        self.armed_ticks = 0
        self.last_clean_ts: float | None = None
        # Janela deslizante de amostras para taxa de faults (timestamp, total).
        self._window: deque[tuple[float, int]] = deque(maxlen=max(2, config.fault_window))
        # REFINAMENTO DO GATE DE FAULTS: exigir N amostras CONSECUTIVAS acima
        # do limite (a mesma disciplina dos ticks de arm) em vez de disparar
        # por uma única rajada de 1s. Contador de provas consecutivas; qualquer
        # amostra abaixo do limite zera (prova precisa ser sustentada).
        self._faults_proof_ticks = 0

    # ------------------------------------------------------------------ update

    def update(self, snap) -> Decision:
        """Processa um snapshot e retorna a decisão do tick."""
        cfg = self.config
        commit_pct = _pct_commit(snap)
        phys_pct = _pct_phys(snap)
        rate, rising = self._register_faults(snap)
        score = self._pressure_score(commit_pct, phys_pct, rate)

        armed_now = commit_pct >= cfg.commit_arm or phys_pct >= cfg.phys_arm
        self.armed_ticks = self.armed_ticks + 1 if armed_now else 0

        if not armed_now:
            # Caiu abaixo do limiar de armar (ou nunca armou): nada a fazer.
            self.armed_ticks = 0
            return Decision(
                action="disarm",
                reason=(
                    f"pressão abaixo do arm: commit {commit_pct:.1f}% < {cfg.commit_arm}% "
                    f"e físico {phys_pct:.1f}% < {cfg.phys_arm}%"
                ),
                profile=cfg.name,
                pressure_score=score,
                armed_ticks=0,
            )

        # Armado a partir daqui. Cooldown tem precedência sobre o disparo.
        in_cooldown = self._in_cooldown()

        # REFINAMENTO DO GATE: prova de faults só vale se SUSTENTADA — a taxa
        # precisa ficar acima do limite por `consecutive` amostras seguidas
        # (mesma disciplina dos ticks de arm). Commit >= fire continua sendo
        # prova imediata (é o sinal mais forte — memória virtual esgotando).
        if rate >= cfg.fault_rate_max:
            self._faults_proof_ticks += 1
        else:
            self._faults_proof_ticks = 0
        faults_proof = self._faults_proof_ticks >= cfg.consecutive

        fire_evidence = commit_pct >= cfg.commit_fire or (rate >= cfg.fault_rate_max and rising and faults_proof)
        can_fire = (
            self.armed_ticks >= cfg.consecutive
            and fire_evidence
            and not in_cooldown
        )

        if can_fire:
            reason_detail = (
                f"commit {commit_pct:.1f}% >= {cfg.commit_fire}%"
                if commit_pct >= cfg.commit_fire
                else (
                    f"hard faults {rate:.1f}/s >= {cfg.fault_rate_max}/s, crescente e "
                    f"sustentado por {self._faults_proof_ticks} amostras"
                )
            )
            return Decision(
                action="clean",
                reason=(
                    f"PROVA de pressão real: {reason_detail}; "
                    f"{self.armed_ticks} ticks armados (>= {cfg.consecutive})"
                ),
                profile=cfg.name,
                pressure_score=score,
                armed_ticks=self.armed_ticks,
            )

        if in_cooldown:
            remaining = self._cooldown_remaining()
            return Decision(
                action="cooldown",
                reason=(
                    f"cooldown ativo: faltam {remaining:.0f}s "
                    f"(pressão: commit {commit_pct:.1f}%, físico {phys_pct:.1f}%)"
                ),
                profile=cfg.name,
                pressure_score=score,
                armed_ticks=self.armed_ticks,
            )

        # Armado, sem cooldown, mas sem prova suficiente ainda.
        waiting_reason = (
            f"armado há {self.armed_ticks}/{cfg.consecutive} ticks"
            if self.armed_ticks < cfg.consecutive
            else (
                f"sem prova de pressão real: commit {commit_pct:.1f}% < {cfg.commit_fire}% "
                f"e faults {rate:.1f}/s abaixo do limite {cfg.fault_rate_max}/s "
                "(físico alto sozinho é cache saudável, não limpa)"
            )
        )
        return Decision(
            action="wait",
            reason=waiting_reason,
            profile=cfg.name,
            pressure_score=score,
            armed_ticks=self.armed_ticks,
        )

    # -------------------------------------------------------------- note_clean

    def note_clean(self, ts: float) -> None:
        """Registra o instante da última limpeza (inicia o cooldown)."""
        self.last_clean_ts = float(ts)

    # ---------------------------------------------------------------- internals

    def _register_faults(self, snap) -> tuple[float, bool]:
        """Atualiza a janela de faults e retorna (taxa/s, crescente?).

        taxa = delta(hard_faults_total)/delta(timestamp) entre as duas
        amostras mais recentes da janela. Crescente = cada delta da janela
        >= o anterior (tolerância de igualdade). Primeira amostra da janela
        (ou dt<=0/contador zificado) => taxa 0.0, não crescente.
        """
        self._window.append((float(snap.timestamp), int(snap.hard_faults_total)))
        if len(self._window) < 2:
            return 0.0, False

        # Taxa entre as duas amostras mais recentes.
        (t0, f0), (t1, f1) = self._window[-2], self._window[-1]
        dt = t1 - t0
        rate = 0.0 if dt <= 0 else max(0.0, (f1 - f0) / dt)

        # Série de deltas dentro da janela (faults por intervalo).
        deltas = [
            self._window[i + 1][1] - self._window[i][1]
            for i in range(len(self._window) - 1)
        ]
        if len(deltas) < 2:
            return rate, False
        rising = all(deltas[i + 1] >= deltas[i] for i in range(len(deltas) - 1))
        return rate, rising

    def _in_cooldown(self) -> bool:
        return self._cooldown_remaining() > 0.0

    def _cooldown_remaining(self) -> float:
        if self.last_clean_ts is None:
            return 0.0
        elapsed = self.now_fn() - self.last_clean_ts
        return max(0.0, float(self.config.cooldown_s) - elapsed)

    # ------------------------------------------------------------ pressure_score

    def _pressure_score(self, commit_pct: float, phys_pct: float, rate: float) -> float:
        """Índice contínuo de pressão 0..1 — FÓRMULA (documentada):

        score = max(
            S_commit,           # provas de pressão real (peso máximo)
            0.5 * S_phys,       # físico sozinho pesa no MÁXIMO metade
            S_faults            # faults acima do limite
        )

        Cada termo é uma rampa clamp(x, 0, 1):
          - S_commit  = commit_pct / commit_fire
            (chega a 1.0 exatamente no limiar de disparo por commit)
          - S_phys    = phys_pct / phys_arm
            (físico sozinho NUNCA leva o score a 1 — o fator 0.5 garante
             que "físico 100%" sem commit/faults pontue <= 0.5)
          - S_faults  = taxa / fault_rate_max, com peso 0.75
            (faults sustentados pesam forte, mas < provas de commit)

        Propriedades: monotônico crescente em cada variável; contínuo;
        score >= 1.0 somente quando há prova de pressão REAL (commit no
        limiar de fire, ou faults no limite) — físico puro não passa de 0.5.
        """
        cfg = self.config

        def ramp(x: float) -> float:
            return max(0.0, min(1.0, x))

        s_commit = ramp(commit_pct / cfg.commit_fire) if cfg.commit_fire > 0 else 0.0
        s_phys = ramp(phys_pct / cfg.phys_arm) if cfg.phys_arm > 0 else 0.0
        s_faults = ramp(rate / cfg.fault_rate_max) if cfg.fault_rate_max > 0 else 0.0

        score = max(s_commit, 0.5 * s_phys, 0.75 * s_faults)
        return round(score, 4)
