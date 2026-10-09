# test_tray_helpers_v02.py — testes das funções v0.2 (% no ícone, plano de
# limpeza agendado, % liberado na última limpeza)

import os
import tempfile
import unittest

from flashmem.tray_helpers import (
    SCHEDULE_OPTIONS,
    balloon_status_text,
    build_menu_items,
    icon_chars_mem,
    last_clean_pct_text,
    schedule_is_due,
    schedule_load,
    schedule_match,
    schedule_persist,
    tooltip_text,
)


class TestIconCharsMem(unittest.TestCase):
    """v0.2.3: o ícone é em BARRAS — icon_chars_mem retorna o nº de barras acesas."""

    def test_barras_proporcionais(self):
        # cada barra ≈ 16,7%: 80% -> 5 barras (exemplo do usuário)
        self.assertEqual(icon_chars_mem(80.0), "5")
        self.assertEqual(icon_chars_mem(0.0), "0")
        self.assertEqual(icon_chars_mem(50.0), "3")
        self.assertEqual(icon_chars_mem(100.0), "6")

    def test_arredondamento(self):
        self.assertEqual(icon_chars_mem(16.0), "1")   # 16/16,7 = 0,96 -> 1
        self.assertEqual(icon_chars_mem(25.0), "2")   # 25/16,7 = 1,5 -> 2
        self.assertEqual(icon_chars_mem(91.0), "5")   # 91/16,7 = 5,46 -> 5
        self.assertEqual(icon_chars_mem(95.0), "6")   # 95/16,7 = 5,7 -> 6

    def test_clamp_100_e_acima_fica_6_barras(self):
        self.assertEqual(icon_chars_mem(100.0), "6")
        self.assertEqual(icon_chars_mem(250.0), "6")

    def test_negativo_clampa_zero(self):
        self.assertEqual(icon_chars_mem(-3.0), "0")


class TestLastCleanPct(unittest.TestCase):
    """v0.2: menu mostra quanto a última limpeza liberou em % da RAM."""

    def test_texto_com_pct_e_gb(self):
        # 2 GiB liberados de 16 GiB = 12,5%
        txt = last_clean_pct_text(2 * 1024**3, 16 * 1024**3)
        self.assertIn("12,5%", txt)
        self.assertIn("2,00 GB", txt)

    def test_negativo_é_honesto(self):
        txt = last_clean_pct_text(-512 * 1024**2, 16 * 1024**3)
        self.assertIn("-", txt)  # sinal preservado

    def test_sem_total_retorna_sem_dados(self):
        self.assertEqual(last_clean_pct_text(1024, 0), "Última limpeza: sem dados")


class TestSchedule(unittest.TestCase):
    """v0.2: plano de limpeza (intervalo / threshold)."""

    def test_off_nunca_dispara(self):
        self.assertFalse(schedule_is_due("off", None, 0.0, 99999.0, 99.0))

    def test_interval_1min_dispara_apos_60s(self):
        self.assertFalse(schedule_is_due("interval", 1, 0.0, 59.0, 50.0))
        self.assertTrue(schedule_is_due("interval", 1, 0.0, 60.0, 50.0))
        self.assertTrue(schedule_is_due("interval", 5, 0.0, 301.0, 50.0))

    def test_interval_respeita_ultima_limpeza(self):
        # limpou há 30s: 5min NÃO deve disparar mesmo com elapsed grande
        self.assertFalse(schedule_is_due("interval", 5, 1000.0, 1030.0, 50.0))
        # limpou há 5min+1s: dispara
        self.assertTrue(schedule_is_due("interval", 5, 1000.0, 1301.0, 50.0))

    def test_threshold_dispara_no_limite(self):
        self.assertFalse(schedule_is_due("threshold", 90, 0.0, 100.0, 89.9))
        self.assertTrue(schedule_is_due("threshold", 90, 0.0, 100.0, 90.0))
        self.assertTrue(schedule_is_due("threshold", 90, 0.0, 100.0, 97.0))

    def test_threshold_ignora_tempo(self):
        # threshold é sobre % atual, não sobre tempo desde a última
        self.assertTrue(schedule_is_due("threshold", 85, 0.0, 1.0, 92.0))

    def test_match_por_id(self):
        self.assertEqual(schedule_match(301), ("interval", 1))
        self.assertEqual(schedule_match(306), ("threshold", 90))
        self.assertEqual(schedule_match(300), ("off", None))
        self.assertIsNone(schedule_match(999))

    def test_persist_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "schedule.json")
            schedule_persist(p, "interval", 5)
            mode, value = schedule_load(p)
            self.assertEqual((mode, value), ("interval", 5))

    def test_load_arquivo_ausente_e_off(self):
        mode, value = schedule_load(os.path.join(tempfile.gettempdir(), "nao_existe_xyz.json"))
        self.assertEqual((mode, value), ("off", None))

    def test_load_corrompido_e_off(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{isso não é json")
            self.assertEqual(schedule_load(p), ("off", None))


class TestMenuV02(unittest.TestCase):
    def test_menu_tem_plano_de_limpeza(self):
        items = build_menu_items("standard", True, ["safe", "standard", "aggressive"])
        ids = [i for i, _, _ in items]
        # todas as opções de agendamento presentes
        for oid, _, _, _ in SCHEDULE_OPTIONS:
            self.assertIn(oid, ids)

    def test_radio_na_opcao_ativa_do_plano(self):
        items = build_menu_items("standard", True, ["safe"], schedule_id=306)
        radios = [(i, fl) for i, lbl, fl in items if i >= 300 and i < 400]
        marcado = [i for i, fl in radios if fl & 1]
        self.assertEqual(marcado, [306])

    def test_item_ultima_limpeza_presente_e_grayed(self):
        items = build_menu_items("standard", True, ["safe"],
                                 last_clean_text="Última limpeza: liberou 10,0% da RAM")
        by_id = {i: (lbl, fl) for i, lbl, fl in items}
        self.assertIn(108, by_id)
        self.assertEqual(by_id[108][1], 2)  # grayed: informativo

    def test_sem_limpeza_sem_item_108(self):
        items = build_menu_items("standard", True, ["safe"])
        ids = [i for i, _, _ in items]
        self.assertNotIn(108, ids)

    def test_menu_compat_sem_argumentos_novos(self):
        # chamadas antigas (3 args) continuam funcionando
        items = build_menu_items("standard", False, ["safe", "standard"])
        self.assertTrue(len(items) > 5)


class TestTooltipV02(unittest.TestCase):
    def test_mem_primeiro(self):
        tip = tooltip_text(79.0, 92.0, 10.0, "wait")
        self.assertIn("mem 92%", tip)
        self.assertIn("commit 79%", tip)

    def test_balloon_status_mem_primeiro(self):
        txt = balloon_status_text(75.0, 99.0, 121.0, 0.9)
        self.assertTrue(txt.startswith("mem 99%"))


if __name__ == "__main__":
    unittest.main()
