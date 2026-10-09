# test_make_icon.py — valida o gerador do ícone oficial (assets/flashmem.ico)

import os
import struct
import tempfile
import unittest

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "make_icon", os.path.join(os.path.dirname(__file__), "..", "tools", "make_icon.py"))
assert _spec is not None and _spec.loader is not None
make_icon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_icon)


def _parse(path):
    with open(path, "rb") as fh:
        d = fh.read()
    res, typ, cnt = struct.unpack("<HHH", d[:6])
    entries = []
    for i in range(cnt):
        w, h, _, _, planes, bits, size, off = struct.unpack(
            "<BBBBHHII", d[6 + 16 * i:6 + 16 * i + 16])
        entries.append((w, h, planes, bits, size, off, d[off:off + size]))
    return res, typ, cnt, entries


class TestMakeIcon(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "icon.ico")
        make_icon.build(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_header_valido(self):
        res, typ, cnt, _ = _parse(self.path)
        self.assertEqual(res, 0)
        self.assertEqual(typ, 1)      # tipo ICO
        self.assertEqual(cnt, 4)      # 16/32/48/256

    def test_quatro_resolucoes(self):
        _, _, _, entries = _parse(self.path)
        sizes = sorted(w or 256 for (w, _h, *_rest) in entries)
        self.assertEqual(sizes, [16, 32, 48, 256])

    def test_dib_32bpp_com_mascara(self):
        _, _, _, entries = _parse(self.path)
        for (w, h, planes, bits, size, off, img) in entries:
            if (w or 256) == 256:
                continue  # 256 é PNG
            self.assertEqual(planes, 1)
            self.assertEqual(bits, 32)
            bsz, iw, ih, p, bpp = struct.unpack("<IiiHH", img[:16])
            self.assertEqual(bsz, 40)
            self.assertEqual((iw, ih), (w, h * 2))  # biHeight = 2*h (máscara)
            self.assertEqual(bpp, 32)
            # pixels + máscara AND 1bpp cabem no size declarado
            mask_rows = ((w + 31) // 32 * 4) * h
            self.assertGreaterEqual(size, 40 + w * h * 4 + mask_rows)

    def test_256_eh_png_valido(self):
        _, _, _, entries = _parse(self.path)
        big = [e for e in entries if (e[0] or 256) == 256][0]
        self.assertTrue(big[6].startswith(b"\x89PNG"))

    def test_icone_do_repo_existe(self):
        repo = os.path.join(os.path.dirname(__file__), "..", "assets", "flashmem.ico")
        self.assertTrue(os.path.isfile(repo), "assets/flashmem.ico deve existir no repo")
        self.assertGreater(os.path.getsize(repo), 10_000)


if __name__ == "__main__":
    unittest.main()
