# -*- coding: utf-8 -*-
"""
flashmem.core.history — histórico de snapshots/decisões em JSONL.

AGENTE B do plano Flash Mem (PLANO_IMPLEMENTACAO.md seção 3).
Formato: JSON Lines (um objeto JSON por linha) — append-only, sem dependências.

Robustez:
- append cria o arquivo se não existir (incl. diretórios pais do caminho dado).
- load em arquivo inexistente retorna [] (primeira execução é caso normal).
- linhas corrompidas (JSON inválido, não-objeto) são PULSADAS sem abortar —
  um registro ruim no meio do arquivo nunca derruba a leitura do histórico.
"""

import json
import os

__all__ = ["append", "load"]


def append(record: dict, path: str = "flashmem_history.jsonl") -> None:
    """Anexa `record` (dict serializável) como uma linha JSON no final de `path`.

    Cria o arquivo (e diretórios pais) se necessário. Serialização falha
    fechando: TypeError de json.dumps propaga ANTES de qualquer escrita —
    nunca deixamos meia-linha no arquivo.
    """
    line = json.dumps(record, ensure_ascii=False)
    dirname = os.path.dirname(os.path.abspath(path))
    os.makedirs(dirname, exist_ok=True)
    # newline="\n" força LF em qualquer SO; "a" = append atômico por linha.
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def load(path: str = "flashmem_history.jsonl", last: int = 20) -> list[dict]:
    """Carrega até `last` registros mais recentes de `path` (ordem de inserção).

    - arquivo inexistente -> [] (comum na primeira execução).
    - linha corrompida -> ignorada (JSON inválido, tipo não-dict, vazia).
    - last=None carrega tudo; last<=0 retorna [] (nada pedido).
    A leitura é única (arquivo inteiro na memória), suficiente para o volume
    do Flash Mem (um registro por tick de monitor).
    """
    if last is not None and last <= 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    records: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue  # linha em branco: ignora
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue  # linha corrompida: pula, não derruba o load
        if isinstance(obj, dict):
            records.append(obj)
        # tipos não-dict (ex. "[1,2]" ou "\"x\"") também são pulados
    if last is None:
        return records
    return records[-last:] if last < len(records) else records
