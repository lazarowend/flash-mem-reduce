# tray_helpers.py — funções PURAS do tray Flash Mem (testáveis sem GUI)
#
# Isoladas AQUI de propósito: a mecânica Win32 (tray.py) é fina e cheia de
# ctypes; estas funções são lógica pura que os testes cobrem sem desktop.
# DESIGN FLASH MEM: cor por PRESSÃO (score do avaliador), NÃO por % físico —
# a diferença filosófica vs. Mem Reduct (que colore por uso de RAM).

from __future__ import annotations


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
    """Dígito exibido no ícone: score*10 truncado (0-9).

    Escolha documentada: o dígito representa a PRESSÃO (0=tranquilo,
    9=pressão máxima), não o % de RAM — coerente com a filosofia do app.
    score >= 1.0 vira '+' (transbordou a escala).
    """
    if score >= 1.0:
        return "+"
    return str(min(9, int(score * 10)))


def tooltip_text(commit_pct: float, phys_pct: float, faults_rate: float,
                 action: str) -> str:
    """Tooltip (szTip, máx 127 chars): resumo honesto em pt-BR."""
    action_label = {
        "clean": "LIMPAR", "wait": "AGUARDAR", "cooldown": "COOLDOWN",
        "disarm": "DESARMADO",
    }.get(action, action.upper())
    txt = (f"Flash Mem — commit {commit_pct:.0f}% | fís {phys_pct:.0f}% | "
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


def build_menu_items(profile_name: str, is_elevated: bool,
                     available: list[str]) -> list[tuple[int, str, int]]:
    """Estrutura do menu de contexto (pura, testável).

    Retorna [(id, rótulo, flags)] — flags: bit 1 = radio marcado,
    bit 2 = desabilitado (grayed). IDs estáveis usados pelo WndProc.
    """
    items: list[tuple[int, str, int]] = [
        (100, "Status (balloon)", 0),
        (101, "Limpar agora (plano/dry-run)", 0),
        (102, "Executar limpeza", 0 if (is_elevated) else 2),
        (0, "", 0),  # separador (id 0)
    ]
    # submenu de perfis: radio no ativo
    for pid_, name in enumerate(available, start=200):
        flags = 1 if name == profile_name else 0
        items.append((pid_, f"Perfil: {name}", flags))
    items.append((0, "", 0))  # separador
    items.append((109, "Sair", 0))
    return items


def balloon_status_text(commit_pct: float, phys_pct: float,
                        standby_mb: float, score: float) -> str:
    """Balloon do item 'Status': resumo do último snapshot."""
    return (f"commit {commit_pct:.0f}% | fís {phys_pct:.0f}% | "
            f"standby {standby_mb:,.0f} MB | score {score:.2f}")[:255]
