# config.py — perfis de configuração do Flash Mem (AGENTE C, Onda 1)
# Lógica PURA: apenas stdlib (json, dataclasses, os). Sem syscalls, sem.ntapi.
# Cada perfil codifica a filosofia do projeto: nunca limpar por % físico sozinho;
# os limiares de commit e de hard faults são a PROVA de pressão real.

from dataclasses import dataclass, asdict, replace
import json
import os

# Chaves de perfil válidas no campo "profile" do JSON do usuário.
_PROFILE_KEYS = ("safe", "standard", "aggressive")
_VALID_PURGE = ("none", "low_prio", "full")
_DEFAULT_PROFILE = "standard"


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    commit_arm: float    # % de commit para ARMAR o avaliador
    commit_fire: float   # % de commit que por si só é prova de pressão
    phys_arm: float      # % físico usado para ARMAR (nunca dispara sozinho)
    consecutive: int      # ticks armados consecutivos exigidos antes de disparar
    fault_rate_max: float   # hard faults/s considerados pressão real
    fault_window: int       # nº de amostras na janela de análise de faults
    cooldown_s: int        # segundos sem novo disparo após note_clean
    trim_top: int           # top-N processos a aparar
    min_ws_mb: int          # working set mínimo (MB) p/ ser candidato
    purge_standby: str      # "none" | "low_prio" | "full"
    flush_modified: bool


# Valores EXATOS do PLANO_IMPLEMENTACAO.md (seção 3).
PROFILES: dict[str, ProfileConfig] = {
    "safe": ProfileConfig(
        name="safe",
        commit_arm=85.0,
        commit_fire=92.0,
        phys_arm=90.0,
        consecutive=3,
        fault_rate_max=5.0,
        fault_window=3,
        cooldown_s=60,
        trim_top=3,
        min_ws_mb=500,
        purge_standby="none",
        flush_modified=False,
    ),
    "standard": ProfileConfig(
        name="standard",
        commit_arm=80.0,
        commit_fire=88.0,
        phys_arm=85.0,
        consecutive=3,
        fault_rate_max=4.0,
        fault_window=3,
        cooldown_s=60,
        trim_top=5,
        min_ws_mb=300,
        purge_standby="low_prio",
        flush_modified=True,
    ),
    "aggressive": ProfileConfig(
        name="aggressive",
        commit_arm=70.0,
        commit_fire=80.0,
        phys_arm=75.0,
        consecutive=2,
        fault_rate_max=3.0,
        fault_window=2,
        cooldown_s=45,
        trim_top=10,
        min_ws_mb=100,
        purge_standby="full",
        flush_modified=True,
    ),
}

# Nome do perfil padrão quando nada é especificado (arquivo ausente/corrompido).
DEFAULT_PROFILE_NAME = _DEFAULT_PROFILE  # alias público, mantido por conveniência


# Sentinela interna: valor incompatível no JSON -> manter o valor do perfil base.
_SKIP = object()


def _coerce_field(key: str, current, value):
    """Coerção tolerante: preserva o tipo do campo do perfil base.

    Retorna _SKIP quando o valor do JSON é incompatível com o campo
    (config corrompida nunca deve quebrar o avaliador).
    """
    if key == "purge_standby":
        # Enum fechado do contrato: valores fora da lista são ignorados.
        return value if value in _VALID_PURGE else _SKIP
    if isinstance(current, bool):
        # bool ANTES de int/float: isinstance(True, int) é True em Python.
        return value if isinstance(value, bool) else _SKIP
    if isinstance(current, int):
        if isinstance(value, bool):
            return _SKIP
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return _SKIP
        return _SKIP
    if isinstance(current, float):
        if isinstance(value, bool):
            return _SKIP
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return _SKIP
        return _SKIP
    if isinstance(current, str):
        return value if isinstance(value, str) else _SKIP
    return _SKIP


def load_config(path: str = "flashmem.json") -> ProfileConfig:
    """Carrega a configuração: merge do JSON do usuário sobre o perfil base.

    - O campo "profile" (opcional) escolhe a base: safe/standard/aggressive.
    - Campos presentes no JSON substituem os da base (merge raso por campo).
    - Tolerante: arquivo inexistente, vazio, JSON inválido, perfil desconhecido
      ou campos com tipo errado -> usa o padrão "standard" / valor da base.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        # Arquivo inexistente, vazio ou JSON inválido -> objeto vazio (padrão).
        data = {}

    base_name = data.get("profile", _DEFAULT_PROFILE)
    if base_name not in _PROFILE_KEYS:
        base_name = _DEFAULT_PROFILE
    base = PROFILES[base_name]

    overrides: dict = {}
    for key in type(base).__dataclass_fields__:
        if key not in data:
            continue
        value = _coerce_field(key, getattr(base, key), data[key])
        if value is not _SKIP:
            overrides[key] = value

    cfg = replace(base, **overrides)
    if "name" not in overrides:
        # Sem "name" no JSON: o nome reflete o perfil base efetivo.
        cfg = replace(cfg, name=base_name)
    return cfg


def save_config(profile: ProfileConfig, path: str) -> None:
    """Salva o perfil como JSON (inclui "profile": name para roundtrip)."""
    payload = asdict(profile)
    payload["profile"] = profile.name
    directory = os.path.dirname(os.fspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def profile_names() -> tuple[str, ...]:
    """Nomes de perfis disponíveis, em ordem fixa (útil para CLI)."""
    return _PROFILE_KEYS
