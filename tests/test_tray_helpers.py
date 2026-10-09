# test_tray_helpers.py — testes das funções PURAS do tray (sem GUI)

import unittest

from flashmem.tray_helpers import (
    balloon_status_text,
    build_menu_items,
    clean_balloon_text,
    icon_digit,
    pressure_bucket,
    tooltip_text,
)


class TestPressureBucket(unittest.TestCase):
    def test_verde_abaixo_de_meio(self):
        self.assertEqual(pressure_bucket(0.0), "verde")
        self.assertEqual(pressure_bucket(0.49), "verde")

    def test_amarelo_meio_a_085(self):
        self.assertEqual(pressure_bucket(0.5), "amarelo")
        self.assertEqual(pressure_bucket(0.84), "amarelo")

    def test_laranja_085_a_1(self):
        self.assertEqual(pressure_bucket(0.85), "laranja")
        self.assertEqual(pressure_bucket(0.99), "laranja")

    def test_vermelho_no_score_maximo(self):
        self.assertEqual(pressure_bucket(1.0), "vermelho")
        self.assertEqual(pressure_bucket(1.5), "vermelho")

    def test_vermelho_quando_decisao_clean(self):
        # decisão de limpar pinta vermelho mesmo com score < 1
        self.assertEqual(pressure_bucket(0.7, "clean"), "vermelho")


class TestIconDigit(unittest.TestCase):
    def test_digitos_escala(self):
        self.assertEqual(icon_digit(0.0), "0")
        self.assertEqual(icon_digit(0.55), "5")
        self.assertEqual(icon_digit(0.97), "9")

    def test_transbordo_vira_mais(self):
        self.assertEqual(icon_digit(1.0), "+")
        self.assertEqual(icon_digit(2.3), "+")


class TestTooltip(unittest.TestCase):
    def test_formato_e_acao(self):
        tip = tooltip_text(79.3, 92.7, 12.4, "wait")
        self.assertIn("commit 79%", tip)
        self.assertIn("fís 93%", tip)
        self.assertIn("AGUARDAR", tip)

    def test_limite_127_chars(self):
        tip = tooltip_text(100.0, 100.0, 99999.0, "cooldown")
        self.assertLessEqual(len(tip), 127)


class TestCleanBalloon(unittest.TestCase):
    def test_com_ganho_e_nota(self):
        txt = clean_balloon_text(2_443_000_000, "ganho apenas contábil", [])
        self.assertIn("MB", txt)
        self.assertIn("contábil", txt)

    def test_negativo_antes_de_tudo_nao_deve_sumir(self):
        txt = clean_balloon_text(-104_857_600, "sem evidência de alívio", [])
        self.assertIn("-", txt)  # sinal preservado honestamente

    def test_falhas_ntstatus_em_hex(self):
        txt = clean_balloon_text(0, "sem evidência", ["0xC0000022"])
        self.assertIn("0xC0000022", txt)


class TestMenu(unittest.TestCase):
    def test_itens_base_na_ordem(self):
        items = build_menu_items("standard", True, ["safe", "standard", "aggressive"])
        ids = [i for i, _, _ in items]
        self.assertEqual(ids[0], 100)  # status
        self.assertIn(109, ids)        # sair

    def test_executar_desabilitado_sem_elevacao(self):
        items = build_menu_items("standard", False, ["safe", "standard", "aggressive"])
        by_id = {i: (lbl, fl) for i, lbl, fl in items}
        self.assertEqual(by_id[102][1], 2)      # desabilitado sem admin
        self.assertEqual(by_id[102][1] & 2, 2)  # bit de disabled ligado

    def test_executar_habilitado_com_elevacao(self):
        items = build_menu_items("standard", True, ["safe", "standard", "aggressive"])
        by_id = {i: (lbl, fl) for i, lbl, fl in items}
        self.assertEqual(by_id[102][1], 0)

    def test_radio_no_perfil_ativo(self):
        items = build_menu_items("aggressive", True, ["safe", "standard", "aggressive"])
        radios = [(i, fl) for i, lbl, fl in items if lbl.startswith("Perfil")]
        marcado = [i for i, fl in radios if fl & 1]
        self.assertEqual(marcado, [202])  # aggressive é o 3º (200+2)

    def test_separadores_presentes(self):
        items = build_menu_items("standard", True, ["safe", "standard"])
        self.assertIn((0, "", 0), items)


class TestStatusBalloon(unittest.TestCase):
    def test_resumo_legivel(self):
        txt = balloon_status_text(75.4, 99.2, 121.4, 0.9)
        self.assertIn("commit 75%", txt)
        self.assertIn("score 0.90", txt)


if __name__ == "__main__":
    unittest.main()
