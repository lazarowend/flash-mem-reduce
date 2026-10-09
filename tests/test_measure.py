# -*- coding: utf-8 -*-
"""
test_measure.py — testes LIVE read-only do AGENTE B (measure + history).

Roda queries reais contra o Windows hospedeiro, mas NUNCA escreve/limpa
memória de terceiros: toda escrita de arquivo usa tempfile em diretório
temporário (história de teste não vaza para o repositório).

Cobertura exigida pelo plano:
- snapshot() executa sem exceção e campos invariantes:
  physical_total > 0 e múltiplo de page_size; physical_available <= total;
  commit_used <= commit_limit; commit_peak >= commit_used;
  hard_faults_total > 0 e monotônico (não decresce); standby_total >= 0.
- history: append+load roundtrip (com campos aninhados), arquivo inexistente
  -> [], linha corrompida é pulada.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flashmem.core.history import append, load
from flashmem.core.measure import MemorySnapshot, snapshot
from flashmem.core import ntapi


class TestSnapshotLive(unittest.TestCase):
    """Snapshot real do sistema — apenas leitura (sem syscall de escrita/limpeza)."""

    @classmethod
    def setUpClass(cls):
        # Um único snapshot por classe: queries NT são baratas, mas os testes
        # de monotonicidade precisam de pares ordenados — capturamos 2 logo.
        cls.snap1 = snapshot()
        cls.snap2 = snapshot()

    def test_snapshot_roda_sem_excecao(self):
        """snapshot() completa e retorna MemorySnapshot frozen com todos os campos."""
        s = self.snap1
        self.assertIsInstance(s, MemorySnapshot)
        # frozen: tentativa de mutação deve levantar FrozenInstanceError
        with self.assertRaises(Exception):
            s.timestamp = 0.0

    def test_physical_total_positivo_e_multiplo_de_page_size(self):
        """physical_total > 0 e múltiplo exato do page_size do sistema."""
        ps = ntapi.query_performance()["page_size"]
        self.assertGreater(self.snap1.physical_total, 0)
        self.assertEqual(self.snap1.physical_total % ps, 0,
                         "physical_total deve ser múltiplo de page_size")

    def test_physical_available_menor_igual_total(self):
        """physical_available <= physical_total (bytes)."""
        self.assertLessEqual(self.snap1.physical_available,
                              self.snap1.physical_total)

    def test_commit_dentro_do_limite(self):
        """commit_used <= commit_limit (oráculo (b) do plano)."""
        self.assertLessEqual(self.snap1.commit_used, self.snap1.commit_limit)

    def test_commit_peak_maior_igual_used(self):
        """commit_peak >= commit_used (pico histórico nunca é menor que o atual)."""
        self.assertGreaterEqual(self.snap1.commit_peak, self.snap1.commit_used)

    def test_hard_faults_positivo_e_monotonico(self):
        """hard_faults_total > 0 e NÃO decresce entre dois snapshots seguidos."""
        self.assertGreater(self.snap1.hard_faults_total, 0)
        self.assertGreaterEqual(self.snap2.hard_faults_total,
                                self.snap1.hard_faults_total,
                                "PageReadCount é cumulativo: nunca diminui")

    def test_standby_nao_negativo(self):
        """standby_total >= 0 (query indisponível -> 0 pelo contrato)."""
        self.assertGreaterEqual(self.snap1.standby_total, 0)

    def test_campos_exatos_do_plano(self):
        """MemorySnapshot tem EXATAMENTE os 15 campos da seção 3 do plano."""
        esperados = {
            "timestamp", "physical_total", "physical_available", "physical_used",
            "standby_total", "standby_low_priority", "modified_list", "zeroed_list",
            "free_list", "commit_used", "commit_limit", "commit_peak",
            "hard_faults_total", "pagefile_used", "pagefile_limit",
        }
        reais = {f for f in MemorySnapshot.__dataclass_fields__}
        self.assertEqual(reais, esperados,
                         "campos divergem do contrato da seção 3")

    def test_snapshot_consistente_interno(self):
        """physical_used == physical_total - physical_available (definição)."""
        s = self.snap1
        self.assertEqual(s.physical_used, s.physical_total - s.physical_available)


class TestHistory(unittest.TestCase):
    """history.append/load com arquivos temporários (nada toca o repo)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "hist.jsonl")
        self.addCleanup(self._tmp.cleanup)

    def test_roundtrip_basico(self):
        """append + load devolve os registros na ordem, com last respeitado."""
        append({"n": 1}, self.path)
        append({"n": 2}, self.path)
        append({"n": 3}, self.path)
        recs = load(self.path, last=20)
        self.assertEqual([r["n"] for r in recs], [1, 2, 3])
        self.assertEqual([r["n"] for r in load(self.path, last=2)], [2, 3])

    def test_roundtrip_campos_aninhados(self):
        """Registro com dict/list aninhados sobrevive ao roundtrip JSON."""
        rec = {
            "snapshot": {
                "physical_total": 16_000_000_000,
                "commit": {"used": 8_000_000, "limit": 20_000_000},
            },
            "decision": {"action": "wait", "reason": "monitorando"},
            "lista": [1, 2, 3],
            "texto_ptbr": "memória estável — acentos ok",
        }
        append(rec, self.path)
        got = load(self.path)[0]
        self.assertEqual(got, rec)

    def test_arquivo_inexistente_retorna_lista_vazia(self):
        """load de caminho inexistente -> [] (não pode explodir na 1a execução)."""
        missing = os.path.join(self._tmp.name, "nunca_criado.jsonl")
        self.assertEqual(load(missing), [])
        self.assertEqual(load(missing, last=5), [])

    def test_linha_corrompida_e_pulada(self):
        """JSON inválido no meio do arquivo não derruba o load da linha seguinte."""
        append({"n": 1}, self.path)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"n": 2, "quebrado' + "\n")   # linha truncada/corrompida
            f.write("isto não é json algum" + "\n")  # lixo puro
            f.write("\n")                            # linha em branco
        append({"n": 3}, self.path)
        recs = load(self.path, last=20)
        self.assertEqual([r["n"] for r in recs], [1, 3],
                         "linhas corrompidas devem ser puladas silenciosamente")

    def test_append_cria_diretorio_pai(self):
        """append em caminho com subdiretório inexistente cria a hierarquia."""
        deep = os.path.join(self._tmp.name, "a", "b", "hist.jsonl")
        append({"ok": True}, deep)
        recs = load(deep)
        self.assertEqual(recs, [{"ok": True}])

    def test_load_last_zero(self):
        """last=0 não retorna registros (pedido explícito de nada)."""
        append({"n": 1}, self.path)
        self.assertEqual(load(self.path, last=0), [])


if __name__ == "__main__":
    unittest.main()
