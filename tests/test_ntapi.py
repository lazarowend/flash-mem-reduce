# -*- coding: utf-8 -*-
"""Testes do flashmem.core.ntapi — oráculos (a)-(e) do PLANO_IMPLEMENTACAO.md seção 4.

Todos read-only em relação ao sistema, exceto (e): empty_working_set no PRÓPRIO
processo (operação segura — documentedada no plano). Nenhum comando Set de limpeza
global é executado aqui (purge/flush ficam a cargo dos testes do agente D).
"""

import os
import unittest
import ctypes
from ctypes import wintypes

from flashmem.core import ntapi


def _global_memory_status_ex() -> "ctypes.Structure":
    """Chama GlobalMemoryStatusEx para usar como oráculo independente."""
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


class TestNtSuccess(unittest.TestCase):
    def test_nt_success_codes_conhecidos(self):
        """NT_SUCCESS: 0x0 é sucesso; 0xC0000004 e 0xC0000061 são falha."""
        self.assertTrue(ntapi.NT_SUCCESS(0))
        self.assertTrue(ntapi.NT_SUCCESS(0x40010000))  # skip informational do Flash Mem
        self.assertFalse(ntapi.NT_SUCCESS(0xC0000004))
        self.assertFalse(ntapi.NT_SUCCESS(0xC0000061))
        self.assertFalse(ntapi.NT_SUCCESS(0xFFFFFFFF))  # wrap de -1 deve ser falha


class TestQueryPerformance(unittest.TestCase):
    """Oráculo (a) e (b): available vs GlobalMemoryStatusEx; invariantes de commit."""

    def setUp(self):
        self.perf = ntapi.query_performance()
        self.gmse = _global_memory_status_ex()

    def test_campos_presentes(self):
        for key in ("available_pages", "committed_pages", "commit_limit",
                    "peak_commit", "page_read_count", "page_size"):
            self.assertIn(key, self.perf, f"campo {key} ausente em query_performance()")

    def test_page_size_sao(self):
        # x64 Windows: página é 4096 bytes (poderia ser maior com ARM64; aceitamos potência de 2 >= 4096)
        self.assertGreaterEqual(self.perf["page_size"], 4096)
        self.assertEqual(self.perf["page_size"] & (self.perf["page_size"] - 1), 0)

    def test_oraculo_a_disponivel_vs_gmse(self):
        """(a) available_bytes == GlobalMemoryStatusEx.ullAvailPhys ±2%."""
        avail_bytes = self.perf["available_pages"] * self.perf["page_size"]
        expected = self.gmse.ullAvailPhys
        if expected <= 0:
            self.skipTest("GlobalMemoryStatusEx retornou 0")
        diff_pct = abs(avail_bytes - expected) / expected * 100.0
        self.assertLessEqual(diff_pct, 2.0,
                             f"available {avail_bytes} difere de GMSE {expected} em {diff_pct:.2f}%")

    def test_oraculo_b_commit_invariantes(self):
        """(b) 0 < commit_used <= commit_limit; commit_peak >= commit_used."""
        used = self.perf["committed_pages"]
        limit = self.perf["commit_limit"]
        peak = self.perf["peak_commit"]
        self.assertGreater(used, 0)
        self.assertLessEqual(used, limit,
                              f"commit_used ({used}) > commit_limit ({limit})")
        self.assertGreaterEqual(peak, used,
                                f"commit_peak ({peak}) < commit_used ({used})")

    def test_page_read_count_nao_negativo(self):
        # PageReadCount é cumulativo desde o boot; só checamos sanidade básica.
        self.assertGreaterEqual(self.perf["page_read_count"], 0)


class TestQueryMemoryLists(unittest.TestCase):
    """Oráculo (d): soma das listas ≈ available ±8% (se a query estiver disponível)."""

    def test_oraculo_d_soma_das_listas(self):
        lists = ntapi.query_memory_lists()
        if lists is None:
            self.skipTest("query_memory_lists indisponível neste build")
        for key in ("zero", "free", "modified", "standby"):
            self.assertIn(key, lists)
        perf = ntapi.query_performance()
        soma_paginas = lists["zero"] + lists["free"] + lists["standby"]
        avail_paginas = perf["available_pages"]
        if avail_paginas <= 0:
            self.skipTest("available_pages == 0")
        diff_pct = abs(soma_paginas - avail_paginas) / avail_paginas * 100.0
        self.assertLessEqual(
            diff_pct, 8.0,
            f"(zero+free+standby)={soma_paginas} difere de available={avail_paginas} "
            f"em {diff_pct:.2f}% (tolerância ±8%)",
        )


class TestQueryPagefile(unittest.TestCase):
    def test_pagefile_limit_e_used_coerentes(self):
        pf = ntapi.query_pagefile()
        self.assertIn("used_bytes", pf)
        self.assertIn("limit_bytes", pf)
        self.assertGreater(pf["limit_bytes"], 0)
        self.assertGreaterEqual(pf["used_bytes"], 0)
        self.assertLessEqual(pf["used_bytes"], pf["limit_bytes"])


class TestEnumProcesses(unittest.TestCase):
    """Oráculo (c): enum contém pid 4 (System) e os.getpid()."""

    def test_oraculo_c_pids_essenciais(self):
        procs = ntapi.enum_processes()
        self.assertGreater(len(procs), 10, "poucos processos enumerados")
        pids = {p["pid"] for p in procs}
        self.assertIn(4, pids, "pid 4 (System) ausente da enumeração")
        self.assertIn(os.getpid(), pids, "próprio pid ausente da enumeração")

    def test_campos_dos_processos(self):
        procs = ntapi.enum_processes()
        by_pid = {p["pid"]: p for p in procs}
        system = by_pid.get(4)
        self.assertIsNotNone(system, "processo System não encontrado")
        self.assertEqual(system["name"].lower(), "system",
                         f"nome do pid 4 deveria ser 'System', veio {system['name']!r}")
        self.assertIsInstance(system["ws_bytes"], int)
        self.assertGreaterEqual(system["ws_bytes"], 0)
        self.assertIsInstance(system["priority"], int)
        # nosso próprio processo também com campos coerentes
        me = by_pid.get(os.getpid())
        self.assertIsNotNone(me)
        self.assertTrue(me["name"], "nome do próprio processo vazio")
        self.assertGreaterEqual(me["ws_bytes"], 0)

    def test_lista_nao_vazia_nomes(self):
        procs = ntapi.enum_processes()
        com_nome = [p for p in procs if p["name"]]
        # System, Registry e vários user-mode devem ter nome; exigimos pelo menos 20
        self.assertGreaterEqual(len(com_nome), 20, "muitos processos sem nome — layout suspeito")


class TestForegroundPid(unittest.TestCase):
    def test_foreground_pid_inteiro_ou_none(self):
        fg = ntapi.get_foreground_pid()
        if fg is not None:
            self.assertIsInstance(fg, int)
            self.assertGreater(fg, 0)


class TestEmptyWorkingSet(unittest.TestCase):
    """Oráculo (e): empty_working_set no próprio pid retorna sucesso (operação segura)."""

    def test_oraculo_e_proprio_processo(self):
        status = ntapi.empty_working_set(os.getpid())
        self.assertEqual(status, 0, f"EmptyWorkingSet no próprio pid falhou: NTSTATUS=0x{status:08X}")

    def test_trim_proprio_processo(self):
        status = ntapi.trim_process(os.getpid())
        self.assertEqual(status, 0, f"SetProcessWorkingSetSize(-1,-1) no próprio pid falhou: 0x{status:08X}")


class TestElevacao(unittest.TestCase):
    def test_is_elevated_bool(self):
        self.assertIsInstance(ntapi.is_elevated(), bool)

    def test_enable_privileges_retorna_lista(self):
        result = ntapi.enable_privileges()
        self.assertIsInstance(result, list)
        for item in result:
            self.assertIn(item, ("SeProfileSingleProcessPrivilege", "SeIncreaseQuotaPrivilege"))

    def test_flush_modified_sem_elevacao_e_skip(self):
        """Sem elevação, flush_modified NÃO executa a syscall e retorna status de skip."""
        if ntapi.is_elevated():
            self.skipTest("sessão elevada — teste de skip não aplicável")
        status = ntapi.flush_modified()
        self.assertEqual(status, ntapi.STATUS_SKIPPED_NOT_ELEVATED,
                         f"esperado skip (0x{ntapi.STATUS_SKIPPED_NOT_ELEVATED:08X}), "
                         f"veio 0x{status:08X}")
        self.assertTrue(ntapi.NT_SUCCESS(status))


if __name__ == "__main__":
    unittest.main(verbosity=2)
