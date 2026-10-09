# -*- coding: utf-8 -*-
"""
flashmem.cli — interface de linha de comando (AGENTE E, Onda 3).

Subcomandos (saída pt-BR, tabular e legível):
  status                snapshot formatado do sistema
  decide                um tick: snapshot + decisão do avaliador
  watch [--interval N] [--execute] [--profile P]   loop do monitor
  clean [--profile P] [--execute]                  plano (dry-run) ou execução
  history [--last N]    últimas limpezas registradas
  tray [--profile P] [--execute]                   ícone na bandeja do Windows

Regras do plano: stdlib pura; dry-run é o DEFAULT; --execute exige confirmação
y/N no stdin; erros de syscall NUNCA silenciados (ntstatus impresso em hex).
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime

# Console do Windows (e .exe do PyInstaller) pode herdar codepage legado
# (cp1252) e quebrar nos caracteres do cabeçalho pt-BR. Forçamos UTF-8 nas
# três saídas padrão o mais cedo possível — resolve .exe e consoles legados.
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # stream já fechado/substituído (ex.: redirecionado a arquivo pelo
        # chamador) — sem problema, o redirecionamento já define o encoding.
        pass

from flashmem.core.config import PROFILES, profile_names
from flashmem.core.decide import PressureEvaluator
from flashmem.core.history import load as history_load
from flashmem.core.measure import snapshot as measure_snapshot

__all__ = ["main", "build_parser"]

_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024
_HISTORY_PATH = "flashmem_history.jsonl"


# --------------------------------------------------------------------------- helpers de formatação

def _fmt_bytes(num: float) -> str:
    """Bytes → legível pt-BR: '7,86 GB' / '512,4 MB' / '1024 KB'."""
    n = float(num)
    if n >= _GB:
        return f"{n / _GB:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " GB"
    if n >= _MB:
        return f"{n / _MB:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".") + " MB"
    if n >= 1024:
        return f"{n / 1024:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".") + " KB"
    return f"{n:,.0f} bytes"


def _pct(part: float, total: float) -> str:
    """Percentual seguro: '84,2%' (0,0 se total<=0)."""
    if total <= 0:
        return "0,0%"
    return f"{(part / total) * 100.0:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".") + "%"


def _pct_value(part: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return (part / total) * 100.0


def _hex_status(st: int) -> str:
    """NTSTATUS em hex 8 dígitos — erros de syscall NUNCA silenciados."""
    return f"0x{int(st) & 0xFFFFFFFF:08X}"


def _out(msg: str = "", stream=None) -> None:
    print(msg, file=stream if stream is not None else sys.stdout)


# --------------------------------------------------------------------------- status

def _cmd_status(args, stream) -> int:
    """Snapshot formatado: físico, standby, listas, commit, pagefile, faults."""
    try:
        snap = measure_snapshot()
    except OSError as exc:
        # Regra do plano: syscall falhou → NTSTATUS visível, nunca silencioso.
        status = getattr(exc, "winerror", None) or getattr(exc, "errno", 0)
        _out(f"ERRO: coleta de snapshot falhou (NTSTATUS {_hex_status(status)})", stream)
        return 1

    commit_pct = _pct_value(snap.commit_used, snap.commit_limit)
    phys_pct = _pct_value(snap.physical_used, snap.physical_total)
    _out("═" * 62, stream)
    _out("  FLASH MEM — status da memória", stream)
    _out("═" * 62, stream)
    _out(f"  Momento da coleta : {datetime.fromtimestamp(snap.timestamp).strftime('%H:%M:%S')}", stream)
    _out("", stream)
    _out("  MEMÓRIA FÍSICA", stream)
    _out(f"    Total        : {_fmt_bytes(snap.physical_total)}", stream)
    _out(f"    Em uso       : {_fmt_bytes(snap.physical_used)}   ({_pct(snap.physical_used, snap.physical_total)})", stream)
    _out(f"    Disponível   : {_fmt_bytes(snap.physical_available)}", stream)
    _out("", stream)
    _out("  LISTAS DE PÁGINAS (PFN)", stream)
    _out(f"    Standby      : {_fmt_bytes(snap.standby_total)}   (cache útil — não é 'livre')", stream)
    if snap.standby_low_priority > 0:
        _out(f"      baixa prio: {_fmt_bytes(snap.standby_low_priority)}", stream)
    _out(f"    Modified     : {_fmt_bytes(snap.modified_list)}", stream)
    _out(f"    Zeroed       : {_fmt_bytes(snap.zeroed_list)}", stream)
    _out(f"    Free         : {_fmt_bytes(snap.free_list)}", stream)
    _out("", stream)
    _out("  COMMIT (memória prometida pelos processos)", stream)
    _out(f"    Em uso       : {_fmt_bytes(snap.commit_used)}   ({_pct(snap.commit_used, snap.commit_limit)})", stream)
    _out(f"    Limite       : {_fmt_bytes(snap.commit_limit)}", stream)
    _out(f"    Pico         : {_fmt_bytes(snap.commit_peak)}   ({_pct(snap.commit_peak, snap.commit_limit)} do limite)", stream)
    _out("", stream)
    _out("  PAGEFILE", stream)
    _out(f"    Em uso       : {_fmt_bytes(snap.pagefile_used)}   ({_pct(snap.pagefile_used, snap.pagefile_limit)})", stream)
    _out(f"    Limite       : {_fmt_bytes(snap.pagefile_limit)}", stream)
    _out("", stream)
    _out("  HARD FAULTS (leitura de disco p/ repor páginas — cumulativo)", stream)
    _out(f"    Total        : {snap.hard_faults_total:,}".replace(",", ".") + " desde o boot", stream)
    _out("", stream)
    _out(f"  Resumo: físico {_pct(phys_pct, 100)} | commit {commit_pct:.1f}%".replace(".", ","), stream)
    _out("═" * 62, stream)
    return 0


# --------------------------------------------------------------------------- decide

def _cmd_decide(args, stream) -> int:
    """Um tick do avaliador: snapshot + decisão, motivo, score, armed_ticks, perfil."""
    try:
        snap = measure_snapshot()
    except OSError as exc:
        status = getattr(exc, "winerror", None) or getattr(exc, "errno", 0)
        _out(f"ERRO: coleta de snapshot falhou (NTSTATUS {_hex_status(status)})", stream)
        return 1

    profile_name = getattr(args, "profile", "standard") or "standard"
    if profile_name in PROFILES:
        cfg = PROFILES[profile_name]
    else:
        cfg = PROFILES["standard"]
    evaluator = PressureEvaluator(cfg)

    # Duas amostras rapidamente para o avaliador ter taxa de faults/s.
    decision = evaluator.update(snap)

    commit_pct = _pct_value(snap.commit_used, snap.commit_limit)
    phys_pct = _pct_value(snap.physical_used, snap.physical_total)

    _out("═" * 62, stream)
    _out("  FLASH MEM — decisão (1 tick)", stream)
    _out("═" * 62, stream)
    _out(f"  Perfil          : {cfg.name}", stream)
    _out(f"  Commit          : {commit_pct:.1f}% (arma ≥ {cfg.commit_arm}%, dispara ≥ {cfg.commit_fire}%)".replace(".", ","), stream)
    _out(f"  Físico          : {phys_pct:.1f}% (arma ≥ {cfg.phys_arm}% — nunca dispara sozinho)".replace(".", ","), stream)
    _out(f"  Score pressão   : {decision.pressure_score:.3f}".replace(".", ","), stream)
    _out(f"  Ticks armados   : {decision.armed_ticks} (exigidos: {cfg.consecutive})", stream)
    _out("", stream)
    action_label = {
        "clean": "LIMPAR (disparou)",
        "wait": "AGUARDAR (armado, sem prova suficiente)",
        "cooldown": "COOLDOWN (aguardando intervalo pós-limpeza)",
        "disarm": "DESARMADO (pressão abaixo do limiar)",
    }.get(decision.action, decision.action.upper())
    _out(f"  ▶ Decisão       : {action_label}", stream)
    _out(f"  Motivo          : {decision.reason}", stream)
    if decision.action == "clean":
        _out("  (sem --execute em modo watch/clean, isto é apenas uma RECOMENDAÇÃO)", stream)
    _out("═" * 62, stream)
    return 0


# --------------------------------------------------------------------------- watch

def _cmd_watch(args, stream) -> int:
    """Loop do Monitor com uma linha resumida por tick.

    SEM --execute: apenas observa e recomenda (nunca chama clean()).
    COM --execute: executa limpezas quando a decisão for 'clean'.
    Ctrl+C encerra limpo.
    """
    from flashmem.monitor import Monitor  # import tardio evita ciclo

    interval = getattr(args, "interval", None) or 1.0
    profile_name = getattr(args, "profile", "standard") or "standard"
    execute = bool(getattr(args, "execute", False))

    mon = Monitor(profile_name=profile_name, execute=execute)

    _out(f"FLASH MEM watch — perfil {mon.config.name} | intervalo base {interval}s | "
         f"{'EXECUTANDO limpezas' if execute else 'modo observação (recomenda apenas)'}", stream)
    _out("Ctrl+C para encerrar.", stream)

    try:
        while True:
            result = mon.run_once()
            mon.ticks += 1
            snap, decision = result["snapshot"], result["decision"]
            commit_pct = _pct_value(snap.commit_used, snap.commit_limit)
            phys_pct = _pct_value(snap.physical_used, snap.physical_total)
            hora = datetime.now().strftime("%H:%M:%S")
            linha = (f"{hora}  commit {commit_pct:5.1f}%  fís {phys_pct:5.1f}%  "
                     f"score {decision.pressure_score:.2f}  {decision.action}"
                     f"{' (executada)' if result['report'] is not None else ''}")
            _out(linha.replace(".", ","), stream)
            delay = interval if decision.armed_ticks > 0 else 3.0
            mon.sleep_fn(delay)
    except KeyboardInterrupt:
        _out("", stream)
        _out(f"Encerrado pelo usuário após {mon.ticks} ticks.", stream)
    return 0


# --------------------------------------------------------------------------- clean

def _confirm_execute(stream) -> bool:
    """Confirmação y/N no stdin (default N) para --execute."""
    _out("Isto vai aparar working sets e esvaziar listas de cache do sistema.", stream)
    _out("Continuar? (y/N): ", stream, )
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "s", "yes", "sim")


def _cmd_clean(args, stream) -> int:
    """clean [--profile P] [--execute]: plano dry-run (default) ou execução real.

    Dry-run imprime a TABELA de ações que seriam executadas com status
    skipped/dry-run. Com --execute: pede confirmação y/N, executa e imprime
    relatório + verificação de eficácia (verify).
    """
    from flashmem.core.clean import clean as core_clean
    from flashmem.core.measure import snapshot as core_snapshot
    from flashmem.core.verify import verify as core_verify

    profile_name = getattr(args, "profile", "standard") or "standard"
    cfg = PROFILES.get(profile_name, PROFILES["standard"])
    execute = bool(getattr(args, "execute", False))

    try:
        before = core_snapshot()
    except OSError as exc:
        status = getattr(exc, "winerror", None) or getattr(exc, "errno", 0)
        _out(f"ERRO: snapshot pré-limpeza falhou (NTSTATUS {_hex_status(status)})", stream)
        return 1

    if not execute:
        # ---------------- PLANO (dry-run, default) ----------------
        report = core_clean(cfg, dry_run=True, before=before)
        _out("═" * 74, stream)
        _out(f"  FLASH MEM clean — PLANO (dry-run) | perfil: {cfg.name}", stream)
        _out("  Nada foi executado. Ações que SERIAM tomadas:", stream)
        _out("═" * 74, stream)
        if not report.actions:
            _out("  (nenhum candidato aos critérios do perfil neste momento)", stream)
        _out(f"  {'AÇÃO':<20} {'ALVO':<28} {'STATUS':<9} NTSTATUS  DETALHE", stream)
        _out("  " + "-" * 72, stream)
        for act in report.actions:
            _out(f"  {act.kind:<20} {act.target:<28} {act.status:<9} "
                 f"{_hex_status(act.ntstatus) if act.ntstatus else '—':<8} {act.detail}", stream)
        _out("  " + "-" * 72, stream)
        _out(f"  Total de ações planejadas: {len(report.actions)}", stream)
        _out("  Para executar de verdade: flashmem clean --execute", stream)
        _out("═" * 74, stream)
        return 0

    # ---------------- EXECUÇÃO REAL (com confirmação) ----------------
    _out(f"FLASH MEM clean --execute | perfil: {cfg.name}", stream)
    if not _confirm_execute(stream):
        _out("Cancelado pelo usuário.", stream)
        return 1

    # Import do monitor para reaproveitar o registro histórico idêntico.
    from flashmem.core.history import append as history_append

    report = core_clean(cfg, dry_run=False, before=before)
    after = core_snapshot()

    # Taxas antes/depois: aproximação por tick (uma amostra por chamada).
    efficacy = core_verify(before, after, 0.0, 0.0)

    _out("", stream)
    _out("═" * 74, stream)
    _out("  RELATÓRIO DA LIMPEZA", stream)
    _out("═" * 74, stream)
    if not report.actions:
        _out("  (nenhuma ação executada)", stream)
    _out(f"  {'AÇÃO':<20} {'ALVO':<28} {'STATUS':<9} NTSTATUS  DETALHE", stream)
    _out("  " + "-" * 72, stream)
    for act in report.actions:
        _out(f"  {act.kind:<20} {act.target:<28} {act.status:<9} "
             f"{_hex_status(act.ntstatus) if act.ntstatus else '—':<8} {act.detail}", stream)
    _out("  " + "-" * 72, stream)
    _out(f"  Livre contábil   : {_fmt_bytes(report.freed_accounting)}  (Δ físico antes→depois; contábil)", stream)
    _out(f"  Duração          : {report.duration_ms:.1f} ms".replace(".", ","), stream)
    _out("", stream)
    _out(f"  Verificação (verify): {efficacy.note}", stream)
    _out(f"    freed_accounting = {_fmt_bytes(efficacy.freed_accounting)}", stream)
    _out("    Δ hard faults por tick não medido nesta via única (use watch --execute"
         " para taxas antes/depois reais).", stream)
    _out("═" * 74, stream)

    # Histórico (mesmo formato do Monitor, para `history` ler).
    record = {
        "ts": before.timestamp,
        "event": "clean",
        "profile": cfg.name,
        "snapshot": {
            "physical_total": before.physical_total,
            "physical_used": before.physical_used,
            "physical_available": before.physical_available,
            "commit_used": before.commit_used,
            "commit_limit": before.commit_limit,
            "hard_faults_total": before.hard_faults_total,
        },
        "decision": {"action": "manual", "reason": "clean via CLI", "pressure_score": 0.0, "armed_ticks": 0},
        "report": {
            "dry_run": report.dry_run,
            "freed_accounting_bytes": report.freed_accounting,
            "duration_ms": round(report.duration_ms, 3),
            "actions_total": len(report.actions),
            "actions_ok": sum(1 for a in report.actions if a.status == "ok"),
            "actions_skipped": sum(1 for a in report.actions if a.status == "skipped"),
            "actions_failed": sum(1 for a in report.actions if a.status == "failed"),
            "failed_ntstatus_hex": [f"0x{a.ntstatus & 0xFFFFFFFF:08X}" for a in report.actions if a.status == "failed"],
        },
        "verify": {
            "freed_accounting_bytes": efficacy.freed_accounting,
            "note": efficacy.note,
        },
        "faults_rate_before": 0.0,
        "faults_rate_after": 0.0,
    }
    try:
        history_append(record, path=_HISTORY_PATH)
    except OSError as exc:
        _out(f"AVISO: falha ao gravar histórico: {exc}", stream)
    return 0


# --------------------------------------------------------------------------- tray

def _cmd_tray(args, stream) -> int:
    """tray [--profile P] [--execute]: ícone de bandeja com monitor contínuo.

    Bloqueia no loop de mensagens até 'Sair' do menu. Sem --execute apenas
    observa e recomenda (nunca chama clean()); com --execute limpa quando a
    decisão é 'clean' (item de menu 102 ainda exige sessão elevada).
    """
    from flashmem.tray import run_tray  # import tardio: módulo Win32 pesado

    profile_name = getattr(args, "profile", "standard") or "standard"
    execute = bool(getattr(args, "execute", False))
    _out(f"FLASH MEM tray — perfil {profile_name} | "
         f"{'EXECUTANDO limpezas' if execute else 'modo observação (recomenda apenas)'}", stream)
    _out("Ícone na bandeja; clique direito = menu (Sair encerra).", stream)
    return run_tray(profile_name=profile_name, execute=execute)


# --------------------------------------------------------------------------- history

def _cmd_history(args, stream) -> int:
    """history [--last N]: lê o histórico JSONL e imprime últimas limpezas."""
    last = getattr(args, "last", 20) or 20
    records = history_load(path=_HISTORY_PATH, last=last)
    _out("═" * 74, stream)
    _out(f"  FLASH MEM — histórico de limpezas (últimas {min(last, len(records))})", stream)
    _out("═" * 74, stream)
    if not records:
        _out("  (nenhum registro — rode `flashmem clean --execute` ou `watch --execute`)", stream)
        _out("═" * 74, stream)
        return 0
    _out(f"  {'QUANDO':<20} {'PERFIL':<12} {'LIVRE CONTÁBIL':<16} DURAÇÃO   AÇÕES(OK/SKIP/FAIL)", stream)
    _out("  " + "-" * 72, stream)
    for rec in records:
        ts = rec.get("ts")
        try:
            quando = datetime.fromtimestamp(float(ts)).strftime("%d/%m %H:%M:%S")
        except (TypeError, ValueError, OSError):
            quando = "?"
        profile = str(rec.get("profile", "?"))[:12]
        rep = rec.get("report", {}) or {}
        freed = rep.get("freed_accounting_bytes", 0)
        dur = rep.get("duration_ms", 0.0)
        a_ok = rep.get("actions_ok", 0)
        a_sk = rep.get("actions_skipped", 0)
        a_fa = rep.get("actions_failed", 0)
        _out(f"  {quando:<20} {profile:<12} {_fmt_bytes(freed):<16} "
             f"{dur:>8.1f}ms  {a_ok}/{a_sk}/{a_fa}".replace(".", ",", 1), stream)
        ver = rec.get("verify", {}) or {}
        if ver.get("note"):
            _out(f"      ↳ verify: {ver['note']}", stream)
    _out("  " + "-" * 72, stream)
    _out(f"  {len(records)} registro(s).", stream)
    _out("═" * 74, stream)
    return 0


# --------------------------------------------------------------------------- parser / main

def build_parser() -> argparse.ArgumentParser:
    """Parser argparse com subcomandos (padrão de perfil: standard)."""
    parser = argparse.ArgumentParser(
        prog="flashmem",
        description="Flash Mem — gerenciador de memória RAM informado, seletivo e verificado (Windows, stdlib pura).",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="snapshot formatado da memória do sistema")

    p_decide = sub.add_parser("decide", help="um tick: snapshot + decisão do avaliador")
    p_decide.add_argument("--profile", "-p", choices=list(profile_names()), default="standard",
                          help="perfil de limiares (default: standard)")

    p_watch = sub.add_parser("watch", help="loop do monitor; sem --execute apenas recomenda")
    p_watch.add_argument("--interval", "-i", type=float, default=1.0,
                         help="intervalo base em segundos quando armado (default: 1.0; ocioso usa 3.0)")
    p_watch.add_argument("--execute", action="store_true",
                         help="executa limpezas quando a decisão for 'clean' (default: apenas recomenda)")
    p_watch.add_argument("--profile", "-p", choices=list(profile_names()), default="standard",
                         help="perfil de limiares (default: standard)")

    p_clean = sub.add_parser("clean", help="plano de limpeza (dry-run default) ou execução com --execute")
    p_clean.add_argument("--profile", "-p", choices=list(profile_names()), default="standard",
                         help="perfil (default: standard)")
    p_clean.add_argument("--execute", action="store_true",
                         help="executa a limpeza de verdade (exige confirmação y/N)")

    p_hist = sub.add_parser("history", help="últimas limpezas registradas")
    p_hist.add_argument("--last", "-n", type=int, default=20,
                        help="quantos registros exibir (default: 20)")

    p_tray = sub.add_parser(
        "tray", help="ícone na bandeja do Windows (loop; clique direito = menu)")
    p_tray.add_argument("--profile", "-p", choices=list(profile_names()),
                        default="standard", help="perfil (default: standard)")
    p_tray.add_argument("--execute", action="store_true",
                         help="executa limpezas quando a decisão for 'clean' "
                              "(default: apenas recomenda; menu mostra plano em dry-run)")

    return parser


def main(argv=None) -> int:
    """Entry point da CLI. Retorna código de saída do processo."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    stream = sys.stdout
    handlers = {
        "status": _cmd_status,
        "decide": _cmd_decide,
        "watch": _cmd_watch,
        "clean": _cmd_clean,
        "tray": _cmd_tray,
        "history": _cmd_history,
    }
    handler = handlers[args.command]
    try:
        return handler(args, stream)
    except OSError as exc:
        # NTSTATUS nunca silenciado: winerror/errno em hex na saída.
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", 0)
        _out(f"ERRO de syscall: {exc} (NTSTATUS {_hex_status(code)})", stream)
        return 1


if __name__ == "__main__":
    sys.exit(main())
