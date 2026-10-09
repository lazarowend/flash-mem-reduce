# -*- coding: utf-8 -*-
"""
flashmem.tray — casca Win32 da bandeja (AGENTE F, Onda 4).

Filosofia: TODO trabalho de decisão/formatação vive em tray_helpers.py
(funções PURAS, 18/18 testes verdes); aqui fica APENAS a mecânica Win32:
ctypes, structs NOTIFYICONDATAW, message loop, ícones desenhados pixel
a pixel e menu de contexto. stdlib pura — zero pip.

Design do ícone 16x16: cor por PRESSÃO (BUCKET_RGB) + dígito do score
(fonte bitmap manual 3x5). GUID fixo para a bandeja sobreviver a
reinícios sem deixar fantasma na área de notificação.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time

from flashmem import tray_helpers as th
from flashmem.core.config import PROFILES, profile_names
from flashmem.core.decide import PressureEvaluator
from flashmem.core.measure import snapshot as measure_snapshot
from flashmem.core import history

__all__ = ["TrayApp", "run_tray", "TRAY_GUID"]

# --------------------------------------------------------------------------- constantes Win32

WM_APP = 0x8000
WM_TRAY_CALLBACK = WM_APP + 1      # uCallbackMessage da bandeja
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIF_GUID = 0x20

TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000

MIIM_STATE = 0x0001
MIIM_ID = 0x0002
MIIM_TYPE = 0x0010
MIIM_SUBMENU = 0x0004
MFT_STRING = 0x0000
MFT_SEPARATOR = 0x0800
MFT_RADIOCHECK = 0x0200
MFS_CHECKED = 0x0008
MFS_GRAYED = 0x0003  # MFS_DISABLED=0x2 | MFS_GRAYED=0x1

SM_CXSMICON = 49

# IDs de menu (contrato com build_menu_items de tray_helpers)
MENU_STATUS = 100
MENU_CLEAN_DRY = 101
MENU_CLEAN_EXEC = 102
MENU_EXIT = 109
MENU_PROFILE_BASE = 200

TRAY_GUID = "{8B1F2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D}"


# --------------------------------------------------------------------------- structs ctypes

# WNDPROC vem ANTES das structs: WNDCLASSEXW referencia o tipo no _fields_.
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", wt.BYTE * 8),
    ]


def _guid_from_str(s: str) -> GUID:
    """'{8B1F-...}' → struct GUID ctypes (campos hex sem hífens)."""
    hex_ = s.strip("{}").replace("-", "")
    g = GUID()
    g.Data1 = int(hex_[0:8], 16)
    g.Data2 = int(hex_[8:12], 16)
    g.Data3 = int(hex_[12:16], 16)
    for i in range(8):
        g.Data4[i] = int(hex_[16 + i * 2:18 + i * 2], 16)
    return g


class NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW x64 — atenção ao alinhamento: ponteiros após DWORDs."""
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", ctypes.c_void_p),      # HICON = ponteiro/opaque
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uTimeout", wt.UINT),           # union {UINT uTimeout; UINT uVersion;}
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        # x64: hBalloonIcon vem depois do GUID (16 bytes) — alinhamento ok
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


class MENUITEMINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("fMask", wt.UINT),
        ("fType", wt.UINT),
        ("fState", wt.UINT),
        ("wID", wt.UINT),
        ("hSubMenu", wt.HMENU),
        ("hbmpChecked", ctypes.c_void_p),
        ("hbmpUnchecked", ctypes.c_void_p),
        ("dwItemData", ctypes.POINTER(ctypes.c_ulong)),
        ("dwTypeData", wt.LPWSTR),
        ("cch", wt.UINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


# --------------------------------------------------------------------------- funções Win32

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wt.ATOM
_user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_user32.CreateWindowExW.restype = wt.HWND
_user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.DefWindowProcW.restype = LRESULT
_user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_user32.GetMessageW.restype = wt.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
_user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.CreatePopupMenu.argtypes = []
_user32.CreatePopupMenu.restype = wt.HMENU
_user32.InsertMenuItemW.argtypes = [wt.HMENU, wt.UINT, wt.BOOL,
                                   ctypes.POINTER(MENUITEMINFOW)]
_user32.TrackPopupMenuEx.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                    wt.HWND, ctypes.c_void_p]
_user32.TrackPopupMenuEx.restype = wt.BOOL
_user32.SetForegroundWindow.argtypes = [wt.HWND]
_user32.DestroyWindow.argtypes = [wt.HWND]
_user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
_user32.DestroyIcon.argtypes = [wt.HICON]
_user32.GetDesktopWindow.argtypes = []
_user32.GetDesktopWindow.restype = wt.HWND

_shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
_shell32.Shell_NotifyIconW.restype = wt.BOOL

_gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p]
_gdi32.CreateBitmap.restype = ctypes.c_void_p
_gdi32.DeleteObject.argtypes = [wt.HANDLE]
_gdi32.DeleteDC.argtypes = [wt.HDC]
# CreateIconIndirect vive no USER32 (não gdi32) — validado no host.
_user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
_user32.CreateIconIndirect.restype = wt.HICON

_kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wt.HMODULE

# POINT/GetCursorPos: menu de contexto na posição do cursor
# DestroyMenu (user32): libera o menu após TrackPopupMenuEx
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
_user32.DestroyMenu.argtypes = [wt.HMENU]


# --------------------------------------------------------------------------- mini-fonte 3x5

# Glifos 3 colunas x 5 linhas; 'X' = pixel aceso, ' ' = apagado.
_GLYPH_0 = ("XXX", "X X", "X X", "X X", "XXX")
_GLYPH_1 = (" X ", " X ", " X ", " X ", " X ")
_DIGIT_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": _GLYPH_0,
    "1": _GLYPH_1,
    "2": ("XXX", "  X", "XXX", "X  ", "XXX"),
    "3": ("XXX", "  X", "XXX", "  X", "XXX"),
    "4": ("X X", "X X", "XXX", "  X", "  X"),
    "5": ("XXX", "X  ", "XXX", "  X", "XXX"),
    "6": ("XXX", "X  ", "XXX", "X X", "XXX"),
    "7": ("XXX", "  X", "  X", "  X", "  X"),
    "8": ("XXX", "X X", "XXX", "X X", "XXX"),
    "9": ("XXX", "X X", "XXX", "  X", "XXX"),
    "+": (" X ", " X ", "XXX", " X ", " X "),
}
# Normaliza para o formato que _draw_glyph espera (' ' = apagado, qualquer
# outro char = aceso) — os glifos usam 'X' e ' ' apenas.
_DIGIT_GLYPHS = {k: tuple(row.replace("X", "#") for row in g)
                 for k, g in _DIGIT_GLYPHS.items()}


def _draw_glyph(pixels: list[list[tuple[int, int, int]]], glyph: tuple[str, ...],
               color: tuple[int, int, int], x0: int, y0: int) -> None:
    """Pinta o glifo 3x5 em `pixels` (lista de listas RGB) na posição dada."""
    for dy, row in enumerate(glyph):
        for dx, ch in enumerate(row):
            if ch != " ":
                pixels[y0 + dy][x0 + dx] = color


# v0.2.6 (pedido do usuário): RAIO do arquivo assets/bolt.png enviado pelo
# usuário (32x32 RGBA, silhueta) — downscale 2x2 para 16x16 (tools/bolt_mask).
# A máscara é o ALPHA de cada pixel (0..255); a cor é aplicada na renderização.
_BOLT_MASK: list[list[int]] = [
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x21, 0x5d, 0x5d, 0x5d, 0x5d, 0x5d, 0x1b, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0xcc, 0xff, 0xff, 0xff, 0xff, 0xf9, 0x26, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x75, 0xff, 0xff, 0xff, 0xff, 0xff, 0x82, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x23, 0xf4, 0xff, 0xff, 0xff, 0xff, 0xdb, 0x08, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0xbb, 0xff, 0xff, 0xff, 0xff, 0xfe, 0x46, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x61, 0xff, 0xff, 0xff, 0xff, 0xff, 0xb6, 0x19, 0x18, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x17, 0xed, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xf1, 0x19, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0xa6, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xf0, 0x38, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x42, 0x55, 0x55, 0x8c, 0xff, 0xff, 0xff, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xb5, 0xff, 0xff, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0x2e, 0xfe, 0xff, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x00, 0xa4, 0xff, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x21, 0xfb, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x00, 0x94, 0xf0, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x14, 0xe6, 0x38, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
    [0x00, 0x00, 0x00, 0x0d, 0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
]
_BOLT_COLOR = (255, 221, 0)        # amarelo elétrico (fase visível)
_BOLT_COLOR_DIM = (110, 110, 110)  # cinza (fase esmaecida do pulso)


def _make_icon_pixels(bucket: str, digit: str) -> list[list[tuple[int, int, int] | None]]:
    """16x16 RGBA: BATERIA de 6 barras + RAIO central (v0.2.5 — pedido do usuário).

    - Fundo TRANSPARENTE (None fora de moldura/barras/raio).
    - Moldura fina + barras acesas na cor da pressão; topo LED branco.
    - RAIO amarelo elétrico cruzando o centro — o 'flash' do Flash Mem.
    - bucket 'off' (fase do delay do pulso): versão ESMAECIDA do MESMO
      ícone (moldura cinza, barras apagadas, raio cinza). O ícone
      PERMANECE na bandeja (v0.2.4 usava NIM_DELETE e deslocava os
      ícones vizinhos — o usuário pediu para ele ficar no lugar).

    `digit` = nº de barras acesas ('0'..'6').
    """
    off = bucket == "off"
    rgb = (70, 70, 70) if off else th.BUCKET_RGB.get(bucket, th.BUCKET_RGB["verde"])
    bar_off = (40, 40, 40)
    bolt_color = _BOLT_COLOR_DIM if off else _BOLT_COLOR
    px: list[list[tuple[int, int, int] | None]] = [[None] * 16 for _ in range(16)]
    # moldura da 'bateria': retângulo 1px na cor do bucket (x=0..15, y=0..15)
    for i in range(16):
        px[0][i] = rgb; px[15][i] = rgb; px[i][0] = rgb; px[i][15] = rgb
    # 6 barras de 1px, gap 1: x = 2,4,6,8,10,12 (dentro da moldura)
    lit = max(0, min(6, int(digit)))
    for bar in range(6):
        x = 2 + bar * 2
        is_lit = bar < lit
        color = rgb if is_lit else bar_off
        top = (255, 255, 255) if is_lit else color
        for y in range(2, 15):
            px[y][x] = top if y == 2 else color
    # RAIO do usuário (bolt.png) por cima de tudo, com alpha da máscara:
    # blend da cor em pixels parciais preserva o anti-aliasing do PNG
    # original (o resultado final é opaco — alpha final via máscara AND).
    for by in range(16):
        for bx in range(16):
            a = _BOLT_MASK[by][bx]
            if a == 0:
                continue
            base = px[by][bx]
            af = a / 255.0
            if base is None:
                px[by][bx] = bolt_color
                continue
            r = int(base[0] * (1 - af) + bolt_color[0] * af)
            g = int(base[1] * (1 - af) + bolt_color[1] * af)
            b = int(base[2] * (1 - af) + bolt_color[2] * af)
            px[by][bx] = (r, g, b)
    return px


def _create_icon(bucket: str, digit: str):
    """Cria HICON 16x16 via CreateIconIndirect (bitmap de cor + máscara AND)."""
    pixels = _make_icon_pixels(bucket, digit)
    return _pixels_to_hicon(pixels)


def _create_transparent_icon():
    """HICON 16x16 TOTALMENTE transparente (fase 'off' do pulso v0.2.2).

    Todos os pixels com máscara AND = 1 (não desenha) e alpha 0 — o ícone
    some da bandeja por instantes, chamando atenção ao reaparecer.
    """
    px = [[None] * 16 for _ in range(16)]
    return _pixels_to_hicon(px)


def _pixels_to_hicon(pixels):
    """Converte grade 16x16 (RGB ou None=transparente) em HICON."""
    height = len(pixels)
    width = len(pixels[0])
    # Bitmap de cor: 32bpp BGRA, linha de baixo para cima (bottom-up).
    color_buf = (ctypes.c_ubyte * (width * height * 4))()
    for y in range(height):
        row = pixels[height - 1 - y]  # bottom-up
        for x in range(width):
            off = (y * width + x) * 4
            if row[x] is None:
                # pixel transparente: alpha 0 (a máscara AND também apaga)
                color_buf[off + 3] = 0x00
                continue
            r, g, b = row[x]
            color_buf[off] = b; color_buf[off + 1] = g
            color_buf[off + 2] = r; color_buf[off + 3] = 0xFF
    # Máscara AND monocromática 1bpp (padded até 32 bits por linha): 1 = apagado
    mask_stride = ((width + 31) // 32) * 4
    mask_buf = (ctypes.c_ubyte * (mask_stride * height))()
    for y in range(height):
        row = pixels[height - 1 - y]
        for x in range(width):
            bit = 0x80 >> (x % 8)
            if row[x] is None:
                # transparente: bit em 1 = NÃO desenha na bandeja
                mask_buf[y * mask_stride + x // 8] |= bit
            else:
                mask_buf[y * mask_stride + x // 8] &= ~bit  # 0 = opaco
    hbm_color = _gdi32.CreateBitmap(width, height, 1, 32, color_buf)
    hbm_mask = _gdi32.CreateBitmap(width, height, 1, 1, mask_buf)
    if not hbm_color or not hbm_mask:
        if hbm_color: _gdi32.DeleteObject(hbm_color)
        if hbm_mask: _gdi32.DeleteObject(hbm_mask)
        return None
    info = ICONINFO()
    info.fIcon = True
    info.xHotspot = 0
    info.yHotspot = 0
    info.hbmMask = hbm_mask
    info.hbmColor = hbm_color
    hicon = _user32.CreateIconIndirect(ctypes.byref(info))
    # bitmaps são copiados pelo CreateIconIndirect: pode deletar em seguida
    _gdi32.DeleteObject(hbm_color)
    _gdi32.DeleteObject(hbm_mask)
    return hicon or None


# --------------------------------------------------------------------------- helpers de NID/menu

def _nid(self_tooltip: str = "", hwnd=None, hicon=None, flags: int = 0) -> NOTIFYICONDATAW:
    """NOTIFYICONDATAW zerada + cbSize + campos comuns (guid fixo)."""
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = flags
    nid.uCallbackMessage = WM_TRAY_CALLBACK
    nid.hIcon = hicon
    nid.guidItem = _guid_from_str(TRAY_GUID)
    if self_tooltip:
        # Py3.14: campo WCHAR*N é read/write via atribuição direta de str
        # (truncado em 127 chars = capacidade de szTip).
        nid.szTip = self_tooltip[:127]
    return nid


def _insert_menu_item(hmenu, position: int, item_id: int, label: str,
                      state_flags: int) -> None:
    """Insere item (ou separador quando item_id==0) via InsertMenuItemW."""
    mii = MENUITEMINFOW()
    mii.cbSize = ctypes.sizeof(MENUITEMINFOW)
    if item_id == 0 and not label:
        # separador
        mii.fMask = MIIM_TYPE | MIIM_ID
        mii.fType = MFT_SEPARATOR
        mii.wID = 0
    else:
        mii.fMask = MIIM_STATE | MIIM_ID | MIIM_TYPE
        mii.fType = MFT_STRING
        mii.fState = state_flags
        mii.wID = item_id
        mii.dwTypeData = label
        mii.cch = len(label)
    ok = _user32.InsertMenuItemW(hmenu, position, True, ctypes.byref(mii))
    if not ok:
        raise OSError(ctypes.get_last_error(), "InsertMenuItemW falhou")


# --------------------------------------------------------------------------- TrayApp

_TIMER_ID = 1
_TIMER_MS = 250  # 4 ticks/s — resolução para o pulso (2,0s on / 0,5s off)

# v0.2.2 (pedido do usuário): PULSO do ícone da bandeja.
# Ciclo de 2,5s: 2,0s visível (mostrando o % de RAM) + 0,5s transparente.
# O reaparecer chama a atenção sem notificação intrusiva. A FASE reinicia
# quando o estado muda (cor/dígito), e o tooltip continua sempre visível.
PULSE_VISIBLE_S = 2.0
PULSE_HIDDEN_S = 0.5
_CLASS_NAME = "FlashMemTrayWnd"

# REGISTRO GLOBAL OBRIGATÓRIO (KB: callback ctypes coletado = crash do processo):
# 1) _WNDPROC_KEEPALIVE segura o trampoline WNDPROC criado no RegisterClassEx;
# 2) _APP_BY_HWND roteia mensagens da proc ESTÁTICA à instância viva.
_WNDPROC_KEEPALIVE = None
_APP_BY_HWND: dict[int, "TrayApp"] = {}


def _static_wndproc(hwnd, msg, wparam, lparam):
    """Proc ESTÁTICA: roteia à instância; desconhecida → DefWindowProc."""
    app = _APP_BY_HWND.get(hwnd)
    if app is None:
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return app._wndproc(hwnd, msg, wparam, lparam)


class TrayApp:
    """Casca Win32 da bandeja: janela oculta + loop de mensagens.

    Toda a lógica (buckets, tooltip, menu, balloons) vem de tray_helpers;
    esta classe só faz a mecânica Win32: registra janela, timer 1s, ícones
    em cache por (bucket, dígito), menu de contexto e o loop
    GetMessageW/Translate/Dispatch.
    """

    def __init__(self, profile_name: str = "standard", execute: bool = False):
        self.profile_name = profile_name if profile_name in PROFILES else "standard"
        self.config = PROFILES[self.profile_name]
        self.execute = bool(execute)
        self.evaluator = PressureEvaluator(self.config)
        # Cache de HICON por (bucket, dígito) — criado 1x, destruído no WM_DESTROY.
        self._icon_cache: dict[tuple[str, str], int] = {}
        self._current_key: tuple[str, str] | None = None
        self.hwnd = None
        self._wndproc_ref = None  # MANTÉM referência: GC do callback = crash
        self._added = False
        self._last_decision = None
        self._last_snapshot = None
        # v0.2 — plano de limpeza (agendamento via menu, persistido em JSON)
        self._schedule_path = "flashmem_schedule.json"
        self.schedule_mode, self.schedule_value = th.schedule_load(self._schedule_path)
        self.schedule_id = self._schedule_id_from_mode()
        self.last_clean_ts: float = 0.0  # timestamp da última limpeza (plano+avaliador)
        self._last_clean_text: str = ""   # 'Última limpeza: liberou X% da RAM'
        # v0.2.2 — estado do PULSO do ícone (2,0s on / 0,5s off)
        self._pulse_phase_start: float = time.time()
        self._last_visual_key: tuple | None = None
        self._last_tip: str = ""
        self._transparent_hicon = None  # cache do ícone transparente (1x)

    def _schedule_id_from_mode(self) -> int:
        """Deriva o id do menu (radio) a partir de (mode, value) carregados."""
        for oid, mode, value, _label in th.SCHEDULE_OPTIONS:
            if mode == self.schedule_mode and value == self.schedule_value:
                return oid
        return 300  # 'off' default

    # ------------------------------------------------------------------ win32 setup

    def _register_class(self) -> bool:
        """Registra a classe com proc ESTÁTICA + keepalive global."""
        global _WNDPROC_KEEPALIVE
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = WNDPROC(_static_wndproc)
        _WNDPROC_KEEPALIVE = wc.lpfnWndProc  # GC do callback = crash
        wc.hInstance = _kernel32.GetModuleHandleW(None)
        wc.lpszClassName = _CLASS_NAME
        atom = _user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS: reuso é ok
                print(f"tray: RegisterClassExW falhou (err {err})",
                      file=sys.stderr)
                return False
        return True

    def _create_window(self) -> bool:
        """Janela message-only (HWND_MESSAGE): invisível, só recebe mensagens."""
        HWND_MESSAGE = -3  # (HWND)-3: janela invisível que só recebe mensagens
        self.hwnd = _user32.CreateWindowExW(
            0, _CLASS_NAME, "FlashMemTray", 0, 0, 0, 0, 0,
            HWND_MESSAGE, None, _kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            print(f"tray: CreateWindowExW falhou (err {ctypes.get_last_error()})",
                  file=sys.stderr)
            return False
        _APP_BY_HWND[self.hwnd] = self  # roteamento na proc estática
        return True

    def _add_tray_icon(self) -> bool:
        """NIM_ADD com ícone inicial (verde/0) e tooltip provisório."""
        hicon = self._ensure_icon("verde", "0")
        nid = _nid("Flash Mem — iniciando…", hwnd=self.hwnd, hicon=hicon,
                   flags=NIF_MESSAGE | NIF_ICON | NIF_TIP)
        ok = _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if ok:
            self._added = True
        else:
            print(f"tray: Shell_NotifyIconW NIM_ADD falhou (err {ctypes.get_last_error()})",
                  file=sys.stderr)
        return bool(ok)

    # ------------------------------------------------------------------ WndProc

    def _wndproc(self, hwnd, msg, wparam, lparam):
        """Proc da janela oculta — RETORNA sempre; excessões não podem vazar."""
        try:
            if msg == WM_TIMER and wparam == _TIMER_ID:
                self._tick()
                return 0
            if msg == WM_TRAY_CALLBACK:
                # lParam carrega o evento do mouse; wParam = uID da bandeja
                event = lparam & 0xFFFFFFFF
                if event == WM_RBUTTONUP or event == WM_CONTEXTMENU:
                    self._show_menu()
                    return 0
                if event == WM_LBUTTONDBLCLK:
                    self._status_balloon()
                    return 0
            if msg == WM_COMMAND:
                self._on_command(int(wparam) & 0xFFFF)
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._teardown()
                _user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # NUNCA derruba o loop
            print(f"tray: excessão no WndProc ignorada: {exc!r}", file=sys.stderr)
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------ tick (1 s)

    def _tick(self):
        """Snapshot + decisão → tooltip/ícone; clean() quando autorizado."""
        try:
            snap = measure_snapshot()
        except OSError as exc:
            print(f"tray: snapshot falhou (NTSTATUS 0x{exc.winerror & 0xFFFFFFFF:08X})",
                  file=sys.stderr)
            return
        decision = self.evaluator.update(snap)
        self._last_snapshot = snap
        self._last_decision = decision

        commit_pct = 100.0 * snap.commit_used / snap.commit_limit if snap.commit_limit else 0.0
        phys_pct = 100.0 * snap.physical_used / snap.physical_total if snap.physical_total else 0.0
        faults_rate = self._faults_rate()
        bucket = th.pressure_bucket(decision.pressure_score, decision.action)
        # v0.2 (pedido do usuário): o NÚMERO do ícone é o % de mem física
        # em uso; a COR continua por pressão (filosofia do app intacta).
        digit = th.icon_chars_mem(phys_pct)
        tip = th.tooltip_text(commit_pct, phys_pct, faults_rate, decision.action)

        # ---------------- v0.2.4 PULSO: 2,0s visível / 0,5s SUMIDO ----------------
        key = (bucket, digit)
        if key != self._current_key:
            self._current_key = key
            self._pulse_phase_start = time.time()  # estado novo: reinicia visível
        now = time.time()
        elapsed = now - self._pulse_phase_start
        cycle = PULSE_VISIBLE_S + PULSE_HIDDEN_S
        visible_now = (elapsed % cycle) < PULSE_VISIBLE_S  # True = visível

        # v0.2.5 (pedido do usuário): o ícone PERMANECE na bandeja nas duas
        # fases. Antes a fase off removia via NIM_DELETE e os ícones vizinhos
        # deslizavam a cada ciclo (irritante). Agora: fase off = MESMO ícone
        # na versão ESMAECIDA (bucket 'off') — pulso sutil, zero deslocamento.
        visual_key = (key, visible_now)
        if visual_key != self._last_visual_key:
            show_bucket = bucket if visible_now else "off"
            hicon = self._ensure_icon(show_bucket, digit)
            nid = _nid(tip, hwnd=self.hwnd, hicon=hicon,
                       flags=NIF_MESSAGE | NIF_ICON | NIF_TIP)
            if self._added:
                ok = _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            else:
                ok = _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                self._added = bool(ok)
            if not ok:
                print(f"tray: NIM_ADD/MODIFY falhou (err {ctypes.get_last_error()})",
                      file=sys.stderr)
            self._last_visual_key = visual_key
            self._last_tip = tip
        elif tip != self._last_tip and self._added:
            # só tooltip muda: NIM_MODIFY APENAS do szTip — SEM NIF_ICON!
            # (BUG v0.2.2 corrigido: com NIF_ICON e hIcon=NULL o Windows
            #  apagava o ícone da bandeja a cada atualização de tooltip —
            #  daí o ícone 'sumir' e só piscar de vez em quando.)
            nid = _nid(tip, hwnd=self.hwnd, flags=NIF_TIP)
            if not _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
                print(f"tray: NIM_MODIFY (tip) falhou (err {ctypes.get_last_error()})",
                      file=sys.stderr)
            self._last_tip = tip

        # Limpeza automática: SÓ quando usuário autorizou via --execute.
        if decision.action == "clean" and self.execute:
            try:
                self._do_clean(snap, decision)
            except Exception as exc:  # excessão NUNCA mata o loop
                print(f"tray: limpeza falhou (ignorada): {exc!r}", file=sys.stderr)

        # v0.2 — PLANO DE LIMPEZA (intervalo/threshold escolhido no menu):
        # dispara INDEPENDENTE do avaliador (pedido explícito do usuário),
        # mas sob a MESMA trava de segurança (só executa com --execute) e
        # com cooldown implícito pelo last_clean_ts compartilhado.
        if th.schedule_is_due(self.schedule_mode, self.schedule_value,
                             self.last_clean_ts, time.time(), phys_pct):
            if self.execute:
                try:
                    self._do_clean(snap, decision,
                                   motivo="plano de limpeza agendado")
                except Exception as exc:  # excessão NUNCA mata o loop
                    print(f"tray: limpeza do plano falhou (ignorada): {exc!r}",
                          file=sys.stderr)
            else:
                # Sem --execute: recomenda (não executa) — honestidade do app.
                self._show_balloon(
                    "Flash Mem — plano dispararia agora",
                    f"Plano '{self.schedule_mode}' atingido (mem {phys_pct:.0f}%). "
                    "Bandeja em modo observação; use 'flashmem tray --execute'.")

    def _faults_rate(self) -> float:
        """Taxa faults/s da janela interna do avaliador (2 amostras recentes)."""
        window = getattr(self.evaluator, "_window", None)
        if not window or len(window) < 2:
            return 0.0
        (t0, f0), (t1, f1) = window[-2], window[-1]
        dt = t1 - t0
        return 0.0 if dt <= 0 else max(0.0, (f1 - f0) / dt)

    def _do_clean(self, snap, decision, motivo: str = "avaliador") -> None:
        """clean() real + verify + history + balloon com o resultado."""
        from flashmem.core.clean import clean as core_clean
        from flashmem.core.verify import verify as core_verify
        from flashmem.core import history as core_history

        faults_before = self._faults_rate()
        report = core_clean(self.config, dry_run=False, before=snap)
        after = measure_snapshot()
        efficacy = core_verify(snap, after, faults_before, self._faults_rate())
        self.evaluator.note_clean(time.time())
        self.last_clean_ts = time.time()  # v0.2: base do plano (interval/threshold)
        # v0.2 (pedido do usuário): texto do menu com o % liberado na última limpeza
        self._last_clean_text = th.last_clean_pct_text(
            report.freed_accounting, snap.physical_total)
        record = {
            "ts": time.time(),
            "event": "clean",
            "profile": self.config.name,
            "snapshot": {
                "physical_total": snap.physical_total,
                "physical_used": snap.physical_used,
                "physical_available": snap.physical_available,
                "commit_used": snap.commit_used,
                "commit_limit": snap.commit_limit,
                "hard_faults_total": snap.hard_faults_total,
            },
            "decision": {
                "action": decision.action,
                "reason": decision.reason,
                "pressure_score": decision.pressure_score,
                "armed_ticks": decision.armed_ticks,
            },
            "report": {
                "dry_run": False,
                "freed_accounting_bytes": report.freed_accounting,
                "duration_ms": round(report.duration_ms, 3),
                "actions_total": len(report.actions),
                "actions_ok": sum(1 for a in report.actions if a.status == "ok"),
                "actions_skipped": sum(1 for a in report.actions if a.status == "skipped"),
                "actions_failed": sum(1 for a in report.actions if a.status == "failed"),
                "failed_ntstatus_hex": [
                    f"0x{int(a.ntstatus) & 0xFFFFFFFF:08X}"
                    for a in report.actions if a.status == "failed"],
            },
            "verify": {
                "freed_accounting_bytes": efficacy.freed_accounting,
                "note": efficacy.note,
            },
            "faults_rate_before": round(faults_before, 3),
            "faults_rate_after": round(self._faults_rate(), 3),
        }
        core_history.append(record, path="flashmem_history.jsonl")
        failed_hex = [f"0x{int(a.ntstatus) & 0xFFFFFFFF:08X}"
                      for a in report.actions if a.status == "failed"]
        self._show_balloon("Flash Mem — limpeza executada",
                           th.clean_balloon_text(report.freed_accounting,
                                                 efficacy.note, failed_hex))

    # ------------------------------------------------------------------ ícones

    def _ensure_icon(self, bucket: str, digit: str):
        """HICON do cache; cria via CreateIconIndirect se ausente."""
        key = (bucket, digit)
        if key not in self._icon_cache:
            hicon = _create_icon(bucket, digit)
            if hicon:
                self._icon_cache[key] = hicon
            return hicon
        return self._icon_cache[key]

    def _ensure_transparent_icon(self):
        """HICON transparente (fase 'off' do pulso) — criado uma única vez."""
        if self._transparent_hicon is None:
            self._transparent_hicon = _create_transparent_icon()
        return self._transparent_hicon

    # ------------------------------------------------------------------ balloons

    def _show_balloon(self, title: str, text: str) -> None:
        """Balloon via NIM_MODIFY|NIF_INFO; falha não derruba nada."""
        try:
            nid = _nid("", hwnd=self.hwnd, flags=NIF_INFO)
            nid.szInfo = text[:255]
            nid.szInfoTitle = title[:63]
            nid.uTimeout = 10000
            nid.dwInfoFlags = 0  # NIIF_NONE
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as exc:
            print(f"tray: balloon falhou (ignorado): {exc!r}", file=sys.stderr)

    def _status_balloon(self) -> None:
        """Duplo clique: balloon de status do último snapshot."""
        snap, dec = self._last_snapshot, self._last_decision
        if snap is None or dec is None:
            self._show_balloon("Flash Mem", "Ainda sem leitura do sistema.")
            return
        commit_pct = 100.0 * snap.commit_used / snap.commit_limit if snap.commit_limit else 0.0
        phys_pct = 100.0 * snap.physical_used / snap.physical_total if snap.physical_total else 0.0
        standby_mb = snap.standby_total / (1024 * 1024)
        text = th.balloon_status_text(commit_pct, phys_pct, standby_mb,
                                      dec.pressure_score)
        self._show_balloon("Flash Mem — status", text)

    # ------------------------------------------------------------------ menu

    def _show_menu(self) -> None:
        """Menu de contexto na posição do cursor (padrão Win32 de bandeja).

        SetForegroundWindow ANTES e PostMessage(WM_NULL) DEPOIS são
        OBRIGATÓRIOS para o menu fechar quando se clica fora (KB135788).
        """
        from flashmem.core.ntapi import is_elevated
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            for pos, (item_id, label, mflags) in enumerate(
                    th.build_menu_items(self.profile_name, is_elevated(),
                                        list(profile_names()),
                                        schedule_id=self.schedule_id,
                                        last_clean_text=self._last_clean_text)):
                if item_id == 0:
                    _insert_menu_item(hmenu, pos, 0, "", 0)  # separador
                else:
                    state = MFS_CHECKED if mflags & 1 else 0
                    if mflags & 2:
                        state |= MFS_GRAYED
                    _insert_menu_item(hmenu, pos, item_id, label, state)
            pt = wt.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            _user32.SetForegroundWindow(self.hwnd)
            _user32.TrackPopupMenuEx(hmenu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                                     pt.x, pt.y, self.hwnd, None)
        finally:
            _user32.DestroyMenu(hmenu)
        # OBRIGATÓRIO após TrackPopupMenu (senão o menu não fecha fora):
        _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)

    def _on_command(self, item_id: int) -> None:
        """Dispara a ação do menu pelo ID (contrato com build_menu_items)."""
        if item_id == MENU_STATUS:
            self._status_balloon()
        elif item_id == MENU_CLEAN_DRY:
            self._clean_dryrun_balloon()
        elif item_id == MENU_CLEAN_EXEC:
            self._clean_execute()
        elif item_id == MENU_EXIT:
            _user32.DestroyWindow(self.hwnd)
        elif item_id >= 300:  # v0.2 — plano de limpeza (300..307)
            # IMPORTANTE: este ramo vem ANTES do de perfis (>=200), senão os
            # ids 300+ seriam capturados por item_id >= MENU_PROFILE_BASE e a
            # seleção do plano morreria silenciosamente (bug corrigido).
            matched = th.schedule_match(item_id)
            if matched:
                self.schedule_mode, self.schedule_value = matched
                self.schedule_id = item_id
                try:
                    th.schedule_persist(self._schedule_path,
                                        self.schedule_mode, self.schedule_value)
                except OSError as exc:
                    print(f"tray: falha ao gravar plano: {exc!r}", file=sys.stderr)
                label = next((lbl for oid, _m, _v, lbl in th.SCHEDULE_OPTIONS
                              if oid == item_id), "?")
                self._show_balloon("Flash Mem — plano de limpeza",
                                   f"Plano configurado: {label}.")
        elif item_id >= MENU_PROFILE_BASE:
            idx = item_id - MENU_PROFILE_BASE
            names = list(profile_names())
            if 0 <= idx < len(names):
                self.profile_name = names[idx]
                self.config = PROFILES[self.profile_name]
                # Avaliador NOVO: trocar perfil zera armed_ticks/janela —
                # limiares diferentes não herdam estado do perfil anterior.
                self.evaluator = PressureEvaluator(self.config)
                self._show_balloon("Flash Mem — perfil",
                                   f"Perfil alterado para '{self.profile_name}'.")

    def _clean_dryrun_balloon(self) -> None:
        """Item 101: dry-run + balloon com o plano resumido."""
        from flashmem.core.clean import clean as core_clean
        snap = measure_snapshot()
        report = core_clean(self.config, dry_run=True, before=snap)
        summary = []
        for act in report.actions[:6]:
            summary.append(f"{act.kind}:{act.status}")
        if len(report.actions) > 6:
            summary.append(f"+{len(report.actions) - 6} ações")
        txt = (" | ".join(summary)) if summary else "nenhuma ação planejada"
        self._show_balloon("Flash Mem — PLANO (dry-run, nada executado)",
                           txt[:255])

    def _clean_execute(self) -> None:
        """Item 102: executa limpeza real (só se execute e elevado)."""
        from flashmem.core.ntapi import is_elevated
        if not self.execute:
            self._show_balloon(
                "Flash Mem — execução bloqueada",
                "Bandeja iniciada sem --execute; use 'flashmem tray --execute' "
                "para autorizar limpezas reais.")
            return
        if not is_elevated():
            self._show_balloon(
                "Flash Mem — execução bloqueada",
                "Sessão sem elevação: purge/flush retornam 0xC0000061 e o trim "
                "de terceiros falha. Rode como administrador.")
            return
        snap = measure_snapshot()
        self._do_clean(snap, self._last_decision or
                       type("D", (), {"action": "manual", "reason": "menu",
                                      "pressure_score": 0.0, "armed_ticks": 0})())

    # ------------------------------------------------------------------ teardown / run

    def _teardown(self) -> None:
        """WM_DESTROY: remove bandeja, mata timer, libera ícones — sem vazamentos."""
        try:
            if self._added and self.hwnd:
                nid = _nid(hwnd=self.hwnd)
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                self._added = False
            if self.hwnd:
                _user32.KillTimer(self.hwnd, _TIMER_ID)
                _APP_BY_HWND.pop(self.hwnd, None)
            for hicon in self._icon_cache.values():
                if hicon:
                    _user32.DestroyIcon(hicon)
            self._icon_cache.clear()
            if self._transparent_hicon:
                _user32.DestroyIcon(self._transparent_hicon)
                self._transparent_hicon = None
        except Exception as exc:
            print(f"tray: teardown parcial: {exc!r}", file=sys.stderr)

    def run(self) -> int:
        """Setup completo + loop de mensagens; retorna código do WM_QUIT."""
        if not self._register_class():
            return 1
        if not self._create_window():
            return 1
        if not self._add_tray_icon():
            return 1
        if not _user32.SetTimer(self.hwnd, _TIMER_ID, _TIMER_MS, None):
            print("tray: SetTimer falhou — sem ticks (bandeja estática)",
                  file=sys.stderr)
        msg = wt.MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:  # WM_QUIT (0) ou erro (-1)
                return int(msg.wParam) if ret == 0 else 1
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        """Encerra de fora do loop (ex.: teste/sinal): posta WM_CLOSE."""
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


def run_tray(profile_name: str = "standard", execute: bool = False) -> int:
    """Entry point do subcomando `tray` da CLI."""
    app = TrayApp(profile_name=profile_name, execute=execute)
    return app.run()
