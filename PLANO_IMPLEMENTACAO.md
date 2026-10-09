# FLASH MEM v0.1 — PLANO DE IMPLEMENTAÇÃO (contrato dos agentes)

Gerenciador de memória RAM informado, seletivo e verificado — evolução dos achados da análise do Mem Reduct (ver C:/Users/BAN/AppData/Local/Temp/memreduct_analysis/DIAGNOSTICO_CONSOLIDADO.md).

## 0. Decisão técnica
Python 3.14 + ctypes, ZERO dependências externas. Alvo: Windows 10/11 x64 (host: Win10 19045). Todas as NT APIs via ntdll/psapi/kernel32/user32.

## 1. Regras do jogo (TODOS os agentes — obrigatório)
1. SOMENTE stdlib: ctypes, json, dataclasses, unittest, time, argparse, os, sys, struct, collections, typing. NENHUM pip install.
2. Testes com unittest padrão, rodados a partir de `C:/Users/BAN/flashmem` com `python -m unittest tests.test_X -v`. TODOS os testes devem passar antes de finalizar.
3. Comentários e mensagens de usuário em pt-BR. Identificadores em inglês.
4. SEM pesquisas na web. Fontes permitidas: o arquivo local `C:/Users/BAN/AppData/Local/Temp/memreduct_analysis/routine-master/src/ntapi.h` (definições phnt completas do Windows) e experimentação empírica guiada pelos oráculos deste plano.
5. Protocolo anti-beco-sem-saída: máximo 3 tentativas para qualquer incógnita (ex.: layout de struct); depois, documentar a limitação em comentário e seguir com a melhor interpretação. NUNCA gastar mais que 2 minutos na mesma verificação.
6. Erros de syscall NUNCA silenciosos: todo NTSTATUS verificado e propagado no retorno.
7. Entrega = arquivos em disco + testes verdes. Summary final CURTO (~15 linhas): arquivos, testes PASS/FAIL, desvios. NÃO escrever relatório longo no summary.
8. Segurança: nenhuma mutação de sistema sem flag explícita; clean() é dry-run por padrão.

## 2. Estrutura e donos
```
C:/Users/BAN/flashmem/
├── PLANO_IMPLEMENTACAO.md      (este contrato)
├── flashmem/
│   ├── __init__.py             (__version__ = "0.1.0")
│   ├── core/
│   │   ├── __init__.py
│   │   ├── ntapi.py            [AGENTE A] bindings ctypes NT/PSAPI/user32
│   │   ├── measure.py          [AGENTE B] snapshot() -> MemorySnapshot
│   │   ├── history.py          [AGENTE B] JSONL append/load
│   │   ├── decide.py           [AGENTE C] PressureEvaluator/Decision (lógica pura)
│   │   ├── config.py           [AGENTE C] perfis + persistência JSON
│   │   ├── clean.py            [AGENTE D] limpeza seletiva + dry-run
│   │   └── verify.py           [AGENTE D] eficácia pós-limpeza
│   ├── monitor.py              [AGENTE E] loop adaptativo
│   └── cli.py                  [AGENTE E] CLI
└── tests/
    ├── __init__.py
    ├── test_ntapi.py           [A] live read-only + smoke seguro
    ├── test_decide.py          [C] determinístico
    ├── test_measure.py         [B] live read-only
    ├── test_clean.py           [D] dry-run + trim do próprio processo
    └── test_integration.py     [E] pipeline completo dry-run
```

## 3. Contratos de interface (respeitar EXATAMENTE nomes e campos)

### measure.py [B]
```python
@dataclass(frozen=True)
class MemorySnapshot:
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

def snapshot() -> MemorySnapshot
```

### decide.py [C] — lógica PURA (sem syscalls; testável com snapshots sintéticos)
```python
@dataclass(frozen=True)
class Decision:
    action: str           # "clean" | "wait" | "cooldown" | "disarm"
    reason: str          # pt-BR legível
    profile: str          # "safe"|"standard"|"aggressive"
    pressure_score: float # 0.0..1.0
    armed_ticks: int

class PressureEvaluator:
    def __init__(self, config: ProfileConfig, now_fn=time.time): ...
    def update(self, snap: MemorySnapshot) -> Decision
    def note_clean(self, ts: float) -> None
```
REGRAS DE DECISÃO (implementar exatamente):
- armado quando: commit_pct >= config.commit_arm OU phys_pct >= config.phys_arm
- dispara ("clean") quando: armed_ticks >= config.consecutive E evidência de pressão real = (commit_pct >= config.commit_fire OU (taxa_hard_faults >= config.fault_rate_max E taxa crescente nas últimas config.fault_window amostras))
- phys_pct NUNCA dispara sozinho (físico cheio sem commit/faults = cache saudável)
- cooldown: nada dispara antes de config.cooldown_s desde note_clean (action="cooldown" com motivo)
- sem disparo e armado: "wait"; caiu abaixo do arm: "disarm"
- pressure_score: função crescente e contínua combinando commit_pct normalizado vs commit_fire, phys_pct vs phys_arm e taxa de faults (documentar fórmula no código)

### config.py [C]
```python
@dataclass(frozen=True)
class ProfileConfig:
    name: str
    commit_arm: float    # %
    commit_fire: float   # %
    phys_arm: float      # %
    consecutive: int
    fault_rate_max: float   # hard faults/s
    fault_window: int       # amostras
    cooldown_s: int
    trim_top: int           # top-N processos a aparar
    min_ws_mb: int          # WS mínimo p/ ser candidato
    purge_standby: str      # "none"|"low_prio"|"full"
    flush_modified: bool

PROFILES: dict[str, ProfileConfig]
# safe     = commit_arm 85, commit_fire 92, phys_arm 90, consecutive 3, fault_rate_max 5.0, fault_window 3, cooldown_s 60, trim_top 3,  min_ws_mb 500, purge_standby "none",     flush_modified False
# standard = commit_arm 80, commit_fire 88, phys_arm 85, consecutive 3, fault_rate_max 4.0, fault_window 3, cooldown_s 60, trim_top 5,  min_ws_mb 300, purge_standby "low_prio", flush_modified True
# aggressive = commit_arm 70, commit_fire 80, phys_arm 75, consecutive 2, fault_rate_max 3.0, fault_window 2, cooldown_s 45, trim_top 10, min_ws_mb 100, purge_standby "full",  flush_modified True

def load_config(path: str = "flashmem.json") -> ProfileConfig   # merge JSON do usuário sobre "standard"; campo "profile" escolhe base
def save_config(profile: ProfileConfig, path: str) -> None
```

### clean.py [D]
```python
@dataclass
class CleanAction:
    kind: str     # "trim_process"|"purge_standby_low"|"purge_standby_full"|"flush_modified"
    target: str   # "pid:nome" ou "sistema"
    status: str   # "ok"|"skipped"|"failed"
    ntstatus: int # 0 se n/a
    detail: str   # pt-BR

@dataclass
class CleanReport:
    dry_run: bool
    actions: list[CleanAction]
    freed_accounting: int   # delta físico antes/depois (CONTÁBIL — ver diagnóstico)
    duration_ms: float

EXCLUDE_NAMES = {"csrss.exe","wininit.exe","services.exe","lsass.exe","winlogon.exe","dwm.exe",
                 "system","registry","memory compression","explorer.exe","fontdrvhost.exe",
                 "svchost.exe","conhost.exe","python.exe","pythonw.exe","hermes"}

def clean(profile: ProfileConfig, dry_run: bool = True, before: MemorySnapshot | None = None) -> CleanReport
# fluxo: enum_processes -> filtrar (ws >= min_ws_mb, nome não excluído, pid != foreground_pid)
#        -> top trim_top por WS -> EmptyWorkingSet em cada (ou apenas planejar se dry_run)
#        -> purge_standby/flush_modified conforme perfil (skip se não elevado; registrar motivo)
#        -> snapshot() final -> freed_accounting = before.physical_used - after.physical_used
# foreground NUNCA é aparado (ntapi.get_foreground_pid()).
```

### verify.py [D]
```python
@dataclass
class EfficacyReport:
    freed_accounting: int
    note: str  # pt-BR: "alívio real provável" vs "ganho apenas contábil" via delta da taxa de hard faults

def verify(before: MemorySnapshot, after: MemorySnapshot,
           faults_rate_before: float, faults_rate_after: float) -> EfficacyReport
```

### history.py [B]
```python
def append(record: dict, path: str = "flashmem_history.jsonl") -> None
def load(path: str = "flashmem_history.jsonl", last: int = 20) -> list[dict]
```

### cli.py [E] — subcomandos (saída pt-BR, tabular e legível)
`status` (snapshot formatado) | `decide` (um tick: snapshot+decisão) | `watch [--interval N] [--execute]` (loop do monitor; SEM --execute apenas observa e recomenda) | `clean [--profile P] [--execute]` (default dry-run imprime o plano) | `history [--last N]`

### monitor.py [E]
```python
class Monitor:
    def __init__(self, profile_name: str = "standard", execute: bool = False): ...
    def run_once(self) -> dict   # {"snapshot":..., "decision":..., "report":...|None}
    def run_loop(self, interval_s: float = 1.0) -> None
# intervalo adaptativo: 1s quando armado, 3s ocioso; clean só se execute=True e action=="clean";
# histórico + verificação gravados a cada limpeza executada
```

## 4. ntapi.py — especificação [AGENTE A]
API exigida pelos demais módulos (nomes exatos):
```python
NT_SUCCESS(status) -> bool
query_performance() -> dict        # available_pages, committed_pages, commit_limit, peak_commit, page_read_count, page_size
query_memory_lists() -> dict|None # zero, free, modified, standby (páginas); None se falhar
query_pagefile() -> dict          # used_bytes, limit_bytes
enum_processes() -> list[dict]    # {"pid":int, "name":str, "ws_bytes":int, "priority":int}
get_foreground_pid() -> int|None
empty_working_set(pid: int) -> int          # ntstatus
trim_process(pid: int) -> int               # SetProcessWorkingSetSize(-1,-1) fallback
purge_standby(low_only: bool = True) -> int # ntstatus
flush_modified() -> int                     # ntstatus
is_elevated() -> bool
enable_privileges() -> list[str]
```
Âncoras técnicas (VALIDAR empiricamente; conferir contra ntapi.h local):
- Classes: SystemBasicInformation=0, SystemPerformanceInformation=2, SystemFileCacheInformation=21, SystemProcessInformation=5, SystemMemoryListInformation=80, SystemRegistryReconciliationInformation=155 (índices já verificados na análise contra ntapi.h:355-482).
- SYSTEM_BASIC_INFORMATION x64: PageSize@0x0C, NumberOfPhysicalPages@0x10.
- SYSTEM_PERFORMANCE_INFORMATION x64: AvailablePages@0x2C, CommittedPages@0x30, CommitLimit@0x34, PeakCommitment@0x38; PageReadCount: obter layout exato em ntapi.h:1705-1788 e validar por monotonicidade entre duas queries.
- SYSTEM_MEMORY_LIST_COMMAND: enum em ntapi.h:1027-1036.
- Fatos empíricos JÁ validados NESTE host (não re-testar comandos Set): query SystemMemoryListInformation funciona SEM elevação; MemoryEmptyWorkingSets/PurgeStandbyList/PurgeLowPriorityStandbyList retornam 0xC0000061 sem elevação; MemoryFlushModifiedList e SystemRegistryReconciliationInformation retornam 0x0 mesmo sem elevação (sucesso != efeito real).

Oráculos de validação (dentro de test_ntapi.py):
(a) available_bytes == GlobalMemoryStatusEx.ullAvailPhys ±2%
(b) 0 < commit_used <= commit_limit; commit_peak >= commit_used
(c) enum_processes contém pid 4 ("System") e os.getpid()
(d) se query_memory_lists() disponível: (zero+free+standby)*page_size ≈ available_bytes ±8%; se falhar estruturalmente, imprimir ReturnLength + primeiros ULONGs no teste e registrar limitação (máx 3 tentativas de layout)
(e) empty_working_set(GetCurrentProcessId()) retorna sucesso (operação segura no próprio processo)

## 5. Ondas e gates (orquestrador = integrador)
- ONDA 1 (paralela): A (ntapi.py) + C (decide.py/config.py). GATE 1: orquestrador roda test_ntapi + test_decide.
- ONDA 2 (paralela): B (measure.py/history.py) + D (clean.py/verify.py). GATE 2: idem.
- ONDA 3: E (cli.py/monitor.py/test_integration.py/README.md). GATE 3: pipeline completo dry-run + smoke real seguro (trim apenas do próprio processo).
- Correções pequenas: orquestrador faz. Desvios grandes: volta ao agente dono.
