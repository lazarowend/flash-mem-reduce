# test_tray.py — testes da casca Win32 da bandeja (AGENTE F, Onda 4)
#
# Duas camadas:
# 1. SANIDADE das funções puras via tray_helpers (cobertura principal vive
#    em test_tray_helpers.py — aqui só reimportamos e garantimos o contrato
#    usado por tray.py: buckets, dígito, tooltip, balloon, menu).
# 2. SMOKE Win32 (skipável): registra classe + janela message-only +
#    Shell_NotifyIconW NIM_ADD → NIM_DELETE em try/finally. Nunca FAIL por
#    ambiente headless: skipUnless(GetDesktopWindow() != 0, 'sem desktop').

import ctypes
import unittest

from flashmem import tray_helpers as th


class TestContratoPuroUsadoPeloTray(unittest.TestCase):
    """As funções puras que tray.py consome continuam no contrato."""

    def test_buckets_cobrem_toda_escala(self):
        self.assertEqual(th.pressure_bucket(0.0), "verde")
        self.assertEqual(th.pressure_bucket(0.6), "amarelo")
        self.assertEqual(th.pressure_bucket(0.9), "laranja")
        self.assertEqual(th.pressure_bucket(1.2), "vermelho")
        self.assertEqual(th.pressure_bucket(0.1, "clean"), "vermelho")

    def test_digito_0_a_9_e_transbordo(self):
        self.assertEqual(th.icon_digit(0.0), "0")
        self.assertEqual(th.icon_digit(0.55), "5")
        self.assertEqual(th.icon_digit(0.999), "9")
        self.assertEqual(th.icon_digit(1.0), "+")

    def test_todos_os_buckets_tem_rgb(self):
        for bucket in ("verde", "amarelo", "laranja", "vermelho"):
            r, g, b = th.BUCKET_RGB[bucket]
            self.assertTrue(0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255)


class TestTrayModule(unittest.TestCase):
    """Import do módulo tray + invariantes estáticos (sem tocar a bandeja)."""

    def test_import_e_glifos(self):
        from flashmem import tray
        # mini-fonte 3x5: 0-9 e '+'; todo glifo 5 linhas x 3 colunas
        self.assertEqual(sorted(tray._DIGIT_GLYPHS),
                         ["+", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"])
        for glyph in tray._DIGIT_GLYPHS.values():
            self.assertEqual(len(glyph), 5)
            for row in glyph:
                self.assertEqual(len(row), 3)

    def test_pixels_do_icone(self):
        from flashmem import tray
        px = tray._make_icon_pixels("verde", "7")
        self.assertEqual(len(px), 16)
        self.assertEqual(len(px[0]), 16)
        # borda na cor do bucket
        r, g, b = th.BUCKET_RGB["verde"]
        self.assertEqual(px[0][0], (r, g, b))
        # glifo '7' tem 7 pixels acesos em branco (3 da barra + 4 do traço)
        self.assertEqual(sum(1 for row in px for p in row if p == (255, 255, 255)), 7)

    def test_glifo_mais_tem_7_pixels(self):
        from flashmem import tray
        px = tray._make_icon_pixels("vermelho", "+")
        self.assertEqual(sum(1 for row in px for p in row if p == (255, 255, 255)), 7)

    def test_notifyicondata_tamanho_x64(self):
        from flashmem import tray
        # NOTIFYICONDATAW em x64: 976 bytes (sanity de alinhamento)
        self.assertEqual(ctypes.sizeof(tray.NOTIFYICONDATAW), 976)

    def test_guid_parse(self):
        from flashmem import tray
        g = tray._guid_from_str(tray.TRAY_GUID)
        self.assertEqual(g.Data1, 0x8B1F2C3D)
        self.assertEqual(g.Data2, 0x4E5F)
        self.assertEqual(g.Data3, 0x4A6B)


def _desktop_disponivel() -> bool:
    """True se há desktop interativo (GetDesktopWindow != 0)."""
    try:
        from flashmem import tray
        return bool(tray._user32.GetDesktopWindow())
    except Exception:
        return False


@unittest.skipUnless(_desktop_disponivel(), "sem desktop (ambiente headless)")
class TestSmokeWin32(unittest.TestCase):
    """SMOKE: classe + janela message-only + NIM_ADD → NIM_DELETE em try/finally.

    Nunca deixa ícone órfão: teardown em finally. Se qualquer syscall da
    cadeia falhar no ambiente, o teste SKIPA (self.skipTest) em vez de
    falhar — ambientes headless/CI não devem quebrar por causa da bandeja.
    """

    def test_ciclo_de_vida_da_bandeja(self):
        from flashmem import tray as t
        app = t.TrayApp(profile_name="standard", execute=False)
        if not app._register_class():
            self.skipTest("RegisterClassExW indisponível neste ambiente")
        try:
            if not app._create_window():
                self.skipTest("CreateWindowExW indisponível neste ambiente")
            if not app._add_tray_icon():
                self.skipTest("Shell_NotifyIconW indisponível neste ambiente")
            # um tick manual (sem o loop): snapshot real + decisão
            try:
                app._tick()
            except OSError:
                self.skipTest("snapshot do sistema indisponível")
            self.assertIsNotNone(app._last_snapshot)
            self.assertIsNotNone(app._last_decision)
            self.assertIn(app._last_decision.action,
                          ("clean", "wait", "cooldown", "disarm"))
            # ícone em cache com chave (bucket, dígito) válida
            bucket, digit = app._current_key
            self.assertIn(bucket, th.BUCKET_RGB)
            # v0.2: o dígito do ícone é o % de mem física (2 chars, '00'-'99')
            self.assertRegex(digit, r"^\d{2}$")
            self.assertIn(app._current_key, app._icon_cache)
        finally:
            app._teardown()
            t._user32.UnregisterClassW(
                t._CLASS_NAME, t._kernel32.GetModuleHandleW(None))
        # após teardown: sem ícone, sem cache, sem rota de mensagem
        self.assertFalse(app._added)
        self.assertEqual(len(app._icon_cache), 0)

    def test_create_icon_todos_os_pares(self):
        from flashmem import tray as t
        criados = 0
        for bucket in ("verde", "amarelo", "laranja", "vermelho"):
            for digit in "0123456789+":
                hicon = t._create_icon(bucket, digit)
                if not hicon:
                    self.skipTest("CreateIconIndirect indisponível")
                t._user32.DestroyIcon(hicon)
                criados += 1
        self.assertEqual(criados, 44)

    def test_on_command_plano_antes_de_perfis(self):
        """REGRESSÃO v0.2: ids 300+ (plano) NÃO podem cair no ramo de perfis.

        Bug real: 'elif item_id >= MENU_PROFILE_BASE (200)' vinha ANTES do
        'elif item_id >= 300', então clicar em 'A cada 5 minutos' (id 302)
        era interpretado como perfil idx=102 (inexistente) e morria
        silenciosamente — o plano nunca mudava de 'desativado'.
        """
        from flashmem import tray as t
        import tempfile, os, json
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sched.json")
            app = t.TrayApp()
            app._schedule_path = path
            app._show_balloon = lambda *a, **k: None  # sem GUI no teste
            # seleciona 'A cada 5 minutos' (id 302) e '90%' (id 306)
            app._on_command(302)
            self.assertEqual(app.schedule_mode, "interval")
            self.assertEqual(app.schedule_value, 5)
            self.assertEqual(app.schedule_id, 302)
            self.assertEqual(json.load(open(path, encoding="utf-8")),
                             {"mode": "interval", "value": 5})
            app._on_command(306)
            self.assertEqual(app.schedule_mode, "threshold")
            self.assertEqual(app.schedule_value, 90)
            # voltar para 'desativado' também precisa funcionar (id 300)
            app._on_command(300)
            self.assertEqual(app.schedule_mode, "off")
            self.assertIsNone(app.schedule_value)
            # e perfis continuam funcionando (id 201 = 'standard' se existir)
            names = list(t.profile_names())
            if len(names) >= 2:
                app._on_command(200 + names.index("aggressive") if "aggressive" in names else 200)
                self.assertIn(app.profile_name, names)


if __name__ == "__main__":
    unittest.main()
