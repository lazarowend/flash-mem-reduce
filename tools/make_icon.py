# -*- coding: utf-8 -*-
"""
make_icon.py — gera o ícone oficial do Flash Mem (assets/flashmem.ico).

Uso:
    python tools/make_icon.py            # grava assets/flashmem.ico
    python tools/make_icon.py OUT.ico    # grava no caminho dado

O .ico gerado é usado por:
  - install.bat  (IconLocation do atalho na Área de Trabalho/Menu Iniciar)
  - builds futuros do PyInstaller: --icon=assets/flashmem.ico

Design (identidade Flash Mem):
  - fundo quadrado arredondado, gradiente vertical verde → azul-escuro
    (a faixa de cores da bandeja vai do verde ao vermelho por pressão;
     o ícone do APP usa o verde, estado saudável, fundindo ao azul-escuro)
  - relâmpago branco central: "flash" = memória liberada num instante
  - contorno escuro para contraste em fundo claro E escuro
  - cantos com anti-aliasing simples (alpha na borda do rounded-rect)

Formato ICO (stdlib pura — struct/zlib):
  - ICONDIR (6 bytes) + ICONDIRENTRY (16 bytes por imagem)
  - 16/32/48 px: DIB 32bpp BGRA bottom-up + máscara AND 1bpp zerada
  - 256 px: PNG embutido (comprimido com zlib, permitido pela spec)
"""

from __future__ import annotations

import os
import struct
import sys
import zlib

# ---------------------------------------------------------------- design

def _mix(c1, c2, t):
    """Interpolação linear de duas cores RGB."""
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


_TOP = (0x1E, 0x9E, 0x5A)      # verde Flash Mem (topo do gradiente)
_BOTTOM = (0x0D, 0x2B, 0x52)   # azul-escuro (base do gradiente)
_BOLT = (0xFF, 0xFF, 0xFF)     # relâmpago branco
_EDGE = (0x0A, 0x14, 0x1E)     # contorno escuro


def _in_bolt(x: float, y: float, s: float) -> bool:
    """Relâmpago estilizado em coordenadas normalizadas (0..1), escala s.

    Forma clássica: dois paralelogramos que se cruzam no meio.
    """
    u, v = x / s, y / s  # normaliza dentro do quadrado
    # paralelogramo superior (haste desce à direita)
    if 0.18 <= u <= 0.62 and 0.08 <= v <= 0.50 and (u - 0.18) >= (v - 0.08) * 0.55:
        if (u - 0.18) <= (v - 0.08) * 0.55 + 0.30:
            return True
    # paralelogramo inferior (ponta desce à esquerda)
    if 0.30 <= u <= 0.80 and 0.46 <= v <= 0.92 and (0.80 - u) >= (v - 0.46) * 0.42:
        if (0.80 - u) <= (v - 0.46) * 0.42 + 0.34:
            return True
    return False


def _rounded_outside(x: float, y: float, s: float, r: float) -> float:
    """Distância ao arco do rounded-rect; >0 = fora (para o contorno)."""
    dx = max(r - x, x - (s - r), 0.0)
    dy = max(r - y, y - (s - r), 0.0)
    return (dx * dx + dy * dy) ** 0.5


def render(size: int) -> list[list[tuple[int, int, int, int]]]:
    """Renderiza o ícone `size x size` em RGBA (top-down)."""
    s = float(size)
    radius = s * 0.22
    edge = max(1.0, s * 0.045)
    px: list[list[tuple[int, int, int, int]]] = []
    for yy in range(size):
        row: list[tuple[int, int, int, int]] = []
        for xx in range(size):
            x, y = xx + 0.5, yy + 0.5
            d = _rounded_outside(x, y, s, radius)
            if d > radius * 0.85 + edge:
                row.append((0, 0, 0, 0))          # totalmente fora: transparente
                continue
            t = y / s                              # gradiente vertical
            base = _mix(_TOP, _BOTTOM, t)
            if d > radius * 0.55:                  # faixa do contorno
                base = _EDGE
            if _in_bolt(x, y, s):
                base = _BOLT
            # anti-aliasing da borda externa
            alpha = 255
            fade = d - (radius * 0.85 - edge * 0.9)
            if fade > 0 and edge > 0:
                alpha = max(0, min(255, int(255 * (1.0 - fade / (edge * 1.9)))))
            row.append((base[0], base[1], base[2], alpha))
        px.append(row)
    return px


# ---------------------------------------------------------------- embutidos

def _dib_image(px) -> bytes:
    """DIB 32bpp BGRA bottom-up + máscara AND 1bpp zerada (opaco)."""
    h = len(px)
    w = len(px[0])
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                      w * h * 4, 0, 0, 0, 0)
    rows = []
    for row in reversed(px):  # bottom-up
        rows.append(b"".join(struct.pack("<BBBB", b, g, r, a)
                             for (r, g, b, a) in row))
    mask_row = b"\x00" * ((w + 31) // 32 * 4)  # máscara zerada = opaco
    mask = mask_row * h
    return hdr + b"".join(rows) + mask


def _png_image(px) -> bytes:
    """PNG RGBA (zlib) — usado para a entrada de 256 px do .ico."""
    h = len(px)
    w = len(px[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("<BBBB", r, g, b, a)
                                      for (r, g, b, a) in row) for row in px)
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build(out_path: str = os.path.join("assets", "flashmem.ico")) -> str:
    """Gera o .ico multi-resolução; retorna o caminho gravado."""
    entries = []   # (largura, altura, dados)
    for size in (16, 32, 48):
        entries.append((size, size, _dib_image(render(size))))
    big = render(256)
    entries.append((256, 256, _png_image(big)))

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dir_entries = b""
    payload = b""
    for (w, h, data) in entries:
        dir_entries += struct.pack("<BBBBHHII", 0 if w >= 256 else w,
                                   0 if h >= 256 else h, 0, 0, 1, 32,
                                   len(data), offset)
        payload += data
        offset += len(data)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(header + dir_entries + payload)
    return out_path


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("assets", "flashmem.ico")
    build(path)
    print(f"Ícone gerado: {path} ({os.path.getsize(path):,} bytes)")
