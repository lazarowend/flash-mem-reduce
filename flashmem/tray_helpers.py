# tray_helpers.py — funções PURAS do tray Flash Mem (testáveis sem GUI)
#
# Isoladas AQUI de propósito: a mecânica Win32 (tray.py) é fina e cheia de
# ctypes; estas funções são lógica pura que os testes cobrem sem desktop.
# DESIGN FLASH MEM: cor por PRESSÃO (score do avaliador), NÃO por % físico —
# a diferença filosófica vs. Mem Reduct (que colore por uso de RAM).

from __future__ import annotations

import json


def pressure_bucket(score: float, action: str = "") -> str:
    """Classifica a pressão em bucket de cor.

    verde    : score < 0.5           — pressão baixa, nada a fazer
    amarelo  : 0.5 <= score < 0.85   — armado/atenção
    laranja  : 0.85 <= score < 1.0   — quase disparo
    vermelho : score >= 1.0 OU decisão 'clean' — prova de pressão real
    """
    if action == "clean" or score >= 1.0:
        return "vermelho"
    if score >= 0.85:
        return "laranja"
    if score >= 0.5:
        return "amarelo"
    return "verde"


BUCKET_RGB = {
    "verde": (0x20, 0xB2, 0x40),      # green (saudável)
    "amarelo": (0xE8, 0xC0, 0x20),    # amarelo
    "laranja": (0xFF, 0x80, 0x40),    # laranja (memreduct warning)
    "vermelho": (0xEC, 0x1C, 0x24),   # vermelho (memreduct danger)
}


def icon_digit(score: float) -> str:
    """Legado (v0.1): dígito de pressão score*10. score>=1.0 vira '+'.

    Mantido por compatibilidade com testes antigos; v0.2 usa icon_chars_mem.
    """
    if score >= 1.0:
        return "+"
    return str(min(9, int(score * 10)))


def icon_chars_mem(phys_pct: float) -> str:
    """v0.2.3 (pedido do usuário): ícone em BARRAS estilo bateria.

    Retorna o NÚMERO DE BARRAS ACESAS como string ('0'..'6'):
    6 barras = 100% de RAM em uso; cada barra ≈ 16,7%.
    Ex.: 80% -> round(80/16,7)=5 barras; 95% -> 6 barras.
    A COR das barras continua por pressão (pressure_bucket) — a
    filosofia do app não muda: só a REPRESENTAÇÃO visual é em barras.
    """
    bars = round(max(0.0, min(100.0, phys_pct)) / (100.0 / 6.0))
    return str(min(6, max(0, int(bars))))


def tooltip_text(commit_pct: float, phys_pct: float, faults_rate: float,
                 action: str) -> str:
    """Tooltip (szTip, máx 127 chars): resumo honesto em pt-BR."""
    action_label = {
        "clean": "LIMPAR", "wait": "AGUARDAR", "cooldown": "COOLDOWN",
        "disarm": "DESARMADO",
    }.get(action, action.upper())
    txt = (f"Flash Mem — mem {phys_pct:.0f}% | commit {commit_pct:.0f}% | "
           f"faults {faults_rate:.0f}/s | {action_label}")
    return txt[:127]


def clean_balloon_text(freed_bytes: int, verify_note: str,
                       failed_hex: list[str]) -> str:
    """Balloon pós-limpeza: resultado + nota do verify + falhas honestas."""
    mb = freed_bytes / (1024 * 1024)
    sign = "+" if freed_bytes >= 0 else ""
    parts = [f"Livre contábil: {sign}{mb:,.1f} MB", verify_note]
    if failed_hex:
        parts.append("falhas: " + ", ".join(failed_hex))
    return " | ".join(parts)[:255]


def last_clean_pct_text(freed_bytes: int, phys_total: int) -> str:
    """v0.2 (pedido do usuário): quanto a ÚLTIMA limpeza liberou, em % da RAM.

    Ex.: 'Última limpeza: liberou 14,3% da RAM (2,31 GB)'. Exibido no menu
    (item grayed, informativo) e reutilizável em balloons. freed negativo/
    zero é reportado honestamente, sem maquiagem.
    """
    if phys_total <= 0:
        return "Última limpeza: sem dados"
    pct = (freed_bytes / phys_total) * 100.0
    gb = freed_bytes / (1024 ** 3)
    sign = "+" if freed_bytes >= 0 else ""
    # vírgula decimal pt-BR (o resto do app usa o mesmo padrão)
    return (f"Última limpeza: liberou {pct:.1f}% da RAM ({sign}{gb:.2f} GB)"
            ).replace(".", ",")


# ------------------------------------------------------------------ plano de limpeza
# v0.2 (pedido do usuário): agendamento configurável via menu da bandeja.
# "off" = nada automático; "interval" = a cada N minutos;
# "threshold" = quando % físico >= X (ex.: 90%). Equivalente ao
# Autoreduct/AutoreductInterval do Mem Reduct, mas com verify + histórico.

SCHEDULE_OPTIONS = [
    # (id_menu, modo, valor, rótulo) — ids 300+ reservados ao submenu
    (300, "off",       None, "Automático: desativado"),
    (301, "interval",  1,    "A cada 1 minuto"),
    (302, "interval",  5,    "A cada 5 minutos"),
    (303, "interval",  15,   "A cada 15 minutos"),
    (304, "interval",  30,   "A cada 30 minutos"),
    (305, "threshold", 85,   "Quando físico ≥ 85%"),
    (306, "threshold", 90,   "Quando físico ≥ 90%"),
    (307, "threshold", 95,   "Quando físico ≥ 95%"),
]


def schedule_match(option_id: int) -> tuple[str, object] | None:
    """Retorna (modo, valor) da opção de menu, ou None se id inválido."""
    for oid, mode, value, _label in SCHEDULE_OPTIONS:
        if oid == option_id:
            return mode, value
    return None


def schedule_is_due(mode: str, value, last_clean_ts: float, now: float,
                    phys_pct: float) -> bool:
    """Lógica PURA: o plano de limpeza dispara agora?

    - "off": nunca.
    - "interval": dispara se now - last_clean_ts >= value*60.
    - "threshold": dispara se phys_pct >= value (pedido explícito do
      usuário: 'se chegar até 90% de uso').
    """
    if mode == "off" or value is None:
        return False
    if mode == "interval":
        return (now - last_clean_ts) >= float(value) * 60.0
    if mode == "threshold":
        return phys_pct >= float(value)
    return False


def schedule_persist(path: str, mode: str, value) -> None:
    """Grava o plano de limpeza em JSON (propaga OSError ao chamador)."""
    data = {"mode": mode, "value": value}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def schedule_load(path: str) -> tuple[str, object]:
    """Carrega o plano; arquivo ausente/corrompido -> ('off', None)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = data.get("mode", "off")
        value = data.get("value")
        if mode in ("off", "interval", "threshold"):
            return mode, value
    except (OSError, ValueError):
        pass
    return "off", None


def build_menu_items(profile_name: str, is_elevated: bool,
                     available: list[str],
                     schedule_id: int = 300,
                     last_clean_text: str = "") -> list[tuple[int, str, int]]:
    """Estrutura do menu de contexto (pura, testável).

    Retorna [(id, rótulo, flags)] — flags: bit 1 = radio/marcado,
    bit 2 = desabilitado (grayed). IDs estáveis usados pelo WndProc.

    v0.2: item informativo da última limpeza (id 108, grayed) e submenu
    'Plano de limpeza' (ids 300+) com radio na opção ativa.
    """
    items: list[tuple[int, str, int]] = [
        (100, "Status (balloon)", 0),
        (101, "Limpar agora (plano/dry-run)", 0),
        (102, "Executar limpeza", 0 if (is_elevated) else 2),
        (0, "", 0),  # separador (id 0)
    ]
    if last_clean_text:
        items.append((108, last_clean_text, 2))  # grayed: só informativo
        items.append((0, "", 0))
    # submenu de perfis: radio no ativo
    for pid_, name in enumerate(available, start=200):
        flags = 1 if name == profile_name else 0
        items.append((pid_, f"Perfil: {name}", flags))
    items.append((0, "", 0))  # separador
    # v0.2: plano de limpeza (agendamento) — radio na opção ativa
    for oid, _mode, _value, label in SCHEDULE_OPTIONS:
        flags = 1 if oid == schedule_id else 0
        items.append((oid, label, flags))
    items.append((0, "", 0))  # separador
    items.append((109, "Sair", 0))
    return items


def balloon_status_text(commit_pct: float, phys_pct: float,
                        standby_mb: float, score: float) -> str:
    """Balloon do item 'Status': resumo do último snapshot."""
    return (f"mem {phys_pct:.0f}% | commit {commit_pct:.0f}% | "
            f"standby {standby_mb:,.0f} MB | score {score:.2f}")[:255]
