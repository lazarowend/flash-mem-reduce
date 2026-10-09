# Flash Mem v0.1

Gerenciador de memória RAM **informado, seletivo e verificado** para Windows 10/11 x64.

## O que este programa resolve

Limpadores tradicionais agem às cegas: botão apertado ou % atingida → esvaziam tudo,
sempre igual. O Flash Mem resolve os **três defeitos** dessa abordagem:

1. **Decisão cega** → decisão **informada**: consulta o estado real das listas de
   memória (commit, hard faults, standby) antes de decidir. Físico cheio sem prova
   de pressão é cache saudável — e **não** é motivo para limpar.
2. **Limpeza indiscriminada** → limpeza **seletiva**: apara apenas os maiores
   working sets, poupando processos críticos e o app em primeiro plano.
3. **Resultado fingido** → resultado **verificado**: depois de limpar, mede se os
   hard faults caíram de verdade e informa honestamente se o ganho foi real ou
   apenas contábil (páginas que migraram para standby continuam na RAM).

## O que é (e o que NÃO é)

O Flash Mem é um **gerenciador informado** — não um limpador cego.

Limpadores cegos (como o Mem Reduct) esvaziam working sets e dão purge nas listas
de cache sempre que um botão é apertado ou uma % é atingida. O problema: **físico
cheio NÃO é pressão de memória**. No Windows, memória "usada" quase sempre é
**standby list — cache útil** que o sistema reaproveita gratuitamente. Esvaziá-la
gera *hard faults*: o disco é relido para repor páginas que já estavam na RAM.

O Flash Mem só recomenda/executa limpeza com **prova de pressão real**:

- **commit** acima do limiar de disparo (memória efetivamente prometida aos processos), OU
- **taxa de hard faults** alta e crescente (leituras de disco para repor páginas).

Físico alto sozinho **nunca** dispara — apenas arma o avaliador.

## Instalação

### Via git clone

```bat
git clone https://github.com/lazarowend/flash-mem-reduce.git
cd flash-mem-reduce
install.bat
```

O `install.bat` faz tudo: detecta Python, builda `flashmem.exe` com PyInstaller
(ou instala em modo Python se você recusar), copia para `%LOCALAPPDATA%\FlashMem`
e cria atalhos com o ícone oficial na **Área de Trabalho** e no **Menu Iniciar**.
Pergunta se você quer autoiniciar com o Windows.

Requisitos: Windows 10/11 x64 + Python 3.10 ou superior (testado em 3.14).
**Zero `pip install`** para o app em si (apenas stdlib); PyInstaller é opcional
para gerar o .exe.

### Desinstalação

Rode `uninstall.bat` na pasta do projeto: encerra o app, remove `%LOCALAPPDATA%\FlashMem`,
os atalhos (Desktop, Menu Iniciar, Autoinicio) e — se você quiser — faz backup
do seu histórico de limpezas em `Documents\FlashMem-backup`. O código-fonte
clonado não é tocado.

### Alternativa: executável empacotado

Com PyInstaller (opcional) gera-se `dist/flashmem.exe` standalone, que roda sem
Python instalado:

```bat
flashmem.exe status
flashmem.exe tray
```

## Uso — subcomandos

```
python -m flashmem.cli status                    # snapshot formatado do sistema
python -m flashmem.cli decide                    # 1 tick: decisão do avaliador
python -m flashmem.cli decide --profile aggressive
python -m flashmem.cli watch                      # loop; apenas OBSERVA e recomenda
python -m flashmem.cli watch --interval 2 --profile safe
python -m flashmem.cli watch --execute            # loop executando limpezas (Ctrl+C encerra)
python -m flashmem.cli clean                      # PLANO em dry-run (default; nada executa)
python -m flashmem.cli clean --profile aggressive --execute   # executa (confirmação y/N)
python -m flashmem.cli history --last 10          # últimas limpezas registradas
python -m flashmem.cli tray                       # ícone na bandeja (observa e recomenda)
python -m flashmem.cli tray --profile safe --execute   # bandeja executando limpezas
```

### `status`
Snapshot legível: físico total/usado/disponível, standby, modified, zeroed,
free, commit usado/limite/pico (com %), pagefile, hard faults cumulativos.

### `decide`
Um tick do pipeline (snapshot + avaliador): imprime decisão (`clean`/`wait`/
`cooldown`/`disarm`), motivo em pt-BR, score de pressão (0..1), ticks armados
e perfil. É a forma rápida de responder "o sistema precisa de limpeza AGORA?".

### `watch`
Loop do monitor com uma linha por tick (`hora commit% phys% decisão`). O
intervalo é **adaptativo**: 1 s quando armado, 3 s ocioso. Sem `--execute`
apenas recomenda; com `--execute` limpa quando a decisão é `clean`, grava o
histórico e a verificação de eficácia.

### `clean`
Default é **dry-run**: imprime a tabela de ações que *seriam* executadas
(status `skipped`/dry-run), sem tocar no sistema. Com `--execute` pede
confirmação `y/N` no stdin e executa, imprimindo relatório + verificação.

### `tray` — ícone na bandeja
Ícone na área de notificação mostrando o **% de memória física em uso**
(dois dígitos, ex.: `37`), colorido **por pressão** (não por % de RAM — a
diferença central vs. limpadores tradicionais):

| Cor | Significado | Número no ícone |
|---|---|---|
| 🟩 verde | pressão baixa (score < 0,5) | % de mem em uso (00-99) |
| 🟨 amarelo | armado — atenção (0,5-0,85) | % de mem em uso |
| 🟧 laranja | quase disparando (0,85-1,0) | % de mem em uso |
| 🟥 vermelho | pressão real comprovada / limpando | % de mem em uso |

Duplo clique = balloon com resumo do momento; clique direito = menu
(Status, plano dry-run, executar limpeza, trocar Perfil, **Plano de
limpeza** — ver abaixo — e Sair). Sem `--execute` apenas observa; com
`--execute` limpa quando há prova de pressão (com cooldown).

### Plano de limpeza (menu da bandeja)

O submenu **"Plano de limpeza"** configura limpezas automáticas, persistidas
em `flashmem_schedule.json` (sobrevivem ao reinício do app):

| Opção | Comportamento |
|---|---|
| Automático: desativado | Nenhuma limpeza automática (default) |
| A cada 1 / 5 / 15 / 30 minutos | Limpeza por intervalo de tempo |
| Quando físico ≥ 85% / 90% / 95% | Limpeza por % de uso atingido |

Após cada limpeza (manual, do avaliador ou do plano), o menu exibe em
tempo real: **"Última limpeza: liberou X,X% da RAM (Y,YY GB)"** — a mesma
medida honesta (contábil) aparece no balloon de resultado.

> Segurança mantida: o plano só **executa** com `tray --execute`; em modo
> observação ele avisa quando dispararia. Threshold/intervalo disparam
> independente do avaliador de pressão — é um plano manual, explícito.

### `history`
Lê `flashmem_history.jsonl` e imprime as últimas limpezas: quando, perfil,
livre contábil, duração, ações ok/skip/fail e o veredicto do `verify`.

## Perfis

| | `safe` | `standard` (default) | `aggressive` |
|---|---|---|---|
| commit arma / dispara | 85% / 92% | 80% / 88% | 70% / 80% |
| físico arma (nunca dispara) | 90% | 85% | 75% |
| ticks consecutivos | 3 | 3 | 2 |
| hard faults/s máx | 5,0 | 4,0 | 3,0 |
| cooldown | 60 s | 60 s | 45 s |
| trim top-N (WS mín) | 3 (500 MB) | 5 (300 MB) | 10 (100 MB) |
| purge standby | nenhum | baixa prioridade | full |
| flush modified | não | sim | sim |

O processo em **primeiro plano** e nomes críticos (`csrss.exe`, `lsass.exe`,
`dwm.exe`, `svchost.exe`, `explorer.exe`, ...) **nunca** são alvo.

## Como funciona

```
measure ──► decide ──► clean ──► verify
(snapshot)  (pressão:  (seletivo:  (a limpeza
 leitura pura armado +   top-N WS,   ALIVIOU ou só
 NT/PSAPI)  prova real) purge, flush) CONTÁBIL?)
```

1. **measure** — `NtQuerySystemInformation` (leitura pura): páginas físicas,
   commit, hard faults (PageReadCount), listas PFN, pagefile.
2. **decide** — máquina de estados pura: ARMA (commit/físico nos limiares),
   exige N ticks consecutivos + prova real (commit ≥ fire OU faults/s altos e
   crescentes); cooldown após cada limpeza; físico sozinho não dispara.
3. **clean** — seletivo: apenas processos com WS grande, nunca o primeiro plano
   nem críticos; purge standby conforme perfil; dry-run por padrão.
4. **verify** — compara a taxa de hard faults antes/depois: se caiu, "alívio
   real provável"; se só o contador contábil subiu, "ganho apenas contábil
   (páginas migraram para standby; continuam na RAM)".

## Avisos honestos

- **Dry-run é o padrão em tudo.** Nada muta no sistema sem `--execute` explícito
  (+ confirmação y/N no `clean`).
- **Elevação**: purge da standby list e flush da modified list exigem sessão
  **elevada** (administrador). Sem elevação o kernel retorna `0xC0000061`
  (STATUS_PRIVILEGED_INSTRUCTION) — o Flash Mem registra como *skip* com o
  NTSTATUS em hex, nunca silencia. O trim de working set de terceiros
  também costuma exigir elevação.
- **Trim é contábil**: o "livre" reportado é o delta do físico usado antes→depois.
  Páginas aparadas migram majoritariamente para a **standby list** — continuam
  na RAM e podem voltar por hard faults. O `verify` existe exatamente para
  distinguir alívio real de ganho contábil; `freed_accounting` pode até ser
  negativo e é reportado sem maquiagem.
- Erros de syscall **nunca** são silenciados: todo NTSTATUS é verificado e
  impresso em hexadecimal.

## Testes

```
cd flashmem
python -m unittest discover -s tests -v
```

130 testes (leitura pura do sistema, lógica determinística com snapshots
sintéticos, dry-run, pipeline de integração com mocks — nenhuma limpeza real
executada pela suíte; o smoke da bandeja pula graciosamente sem desktop).
