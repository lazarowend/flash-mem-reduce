# bolt_mask.py — extrai a máscara do raio do usuário (bolt.png 32x32 RGBA)
# para uma grade 16x16, pronta para uso no ícone da bandeja e no .ico.
#
# O PNG do usuário é uma SILHUETA (pixels pretos com alpha): a forma é o
# que importa — a cor é aplicada depois (amarelo elétrico na fase acesa,
# cinza na esmaecida). Downscale 32->16 por área 2x2 (média de alpha).

import struct
import zlib


def decode_png_rgba(path: str):
    """Decodifica PNG RGBA 8bpp sem interlace (stdlib pura)."""
    d = open(path, "rb").read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "não é PNG"
    idat = b""
    w = h = 0
    off = 8
    while off < len(d):
        length = struct.unpack(">I", d[off:off + 4])[0]
        tag = d[off + 4:off + 8]
        data = d[off + 8:off + 8 + length]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", data[:10])
            assert depth == 8 and ctype == 6, "esperado RGBA 8bpp"
        elif tag == b"IDAT":
            idat += data
        off += 12 + length
    raw = zlib.decompress(idat)
    stride = w * 4 + 1
    prev = [bytes(w * 4)]
    rows = []
    for y in range(h):
        f = raw[y * stride]
        line = bytearray(raw[y * stride + 1:(y + 1) * stride])
        bpp = 4
        if f == 1:
            for i in range(bpp, len(line)):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(len(line)):
                line[i] = (line[i] + prev[y][i]) & 0xFF
        elif f == 3:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[y][i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                b_ = prev[y][i]
                c = prev[y][i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b_ - c), abs(a - c), abs(a + b_ - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b_ if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev.append(bytes(line))
        rows.append(bytes(line))
    return w, h, rows


def bolt_mask_16(path: str) -> list[list[int]]:
    """Máscara 16x16 do raio: alpha médio de blocos 2x2 (0..255).

    Retorna grade 16x16 com a intensidade do raio por pixel.
    """
    w, h, rows = decode_png_rgba(path)
    assert (w, h) == (32, 32), "esperado 32x32"
    mask = [[0] * 16 for _ in range(16)]
    for y in range(16):
        for x in range(16):
            total = 0
            for dy in (0, 1):
                for dx in (0, 1):
                    a = rows[y * 2 + dy][(x * 2 + dx) * 4 + 3]
                    total += a
            mask[y][x] = total // 4
    return mask


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\BAN\Downloads\bolt.png"
    m = bolt_mask_16(src)
    print("máscara 16x16 (hex do alpha):")
    for row in m:
        print("".join(f"{v:02x} " if v else ".  " for v in row))
