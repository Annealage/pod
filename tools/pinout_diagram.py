#!/usr/bin/env python3
"""Generate the Annealage Pod pinout diagram.

Renders the Raspberry Pi Pico 2 W 40-pin pinout in the familiar box-per-function
style and overlays the pod's own DUT-facing assignments (SWD, nRST, I2C target,
DUT UART, backup REPL, ADC, logic-analyser block) as a highlighted outer column
with VERIFIED/SUGGESTED status.

The pod layer is driven from annealage_pod._rp2_pinmap.pinmap() so the diagram
regenerates from the single source of truth; the stock Pico 2 W pin functions are
a static table transcribed from the Raspberry Pi pinout. Emits an SVG and, when
cairosvg is available, a PNG.

Usage:
    python3 tools/pinout_diagram.py [--svg OUT.svg] [--png OUT.png]
"""

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PINMAP_PY = os.path.join(REPO, "src", "mpy", "annealage_pod", "_rp2_pinmap.py")

# ── category colours (approximating the Raspberry Pi pinout legend) ──────────
C = {
    "power": "#c8322d",
    "ground": "#4d4d4d",
    "uart": "#7d3ac1",
    "gpio": "#3f9c35",
    "adc": "#2f8f5b",
    "spi": "#c0398f",
    "i2c": "#2f6fb0",
    "sysctl": "#d98cae",
    "debug": "#e07b2f",
    "pod": "#e8b23a",   # Annealage Pod assignment highlight
}
BG = "#0f1012"
BOARD = "#1f7a3d"
BOARD_EDGE = "#14562a"
TEXT_LIGHT = "#ffffff"
TEXT_DARK = "#111111"

# ── stock Pico 2 W pin table (physical pin 1..40) ────────────────────────────
# Each entry: pin, kind, gpio (or None), name (for non-gpio), alts list of
# (category, text) drawn outward from the board, adc text (green) if any.
def _g(pin, gpio, alts, adc=None):
    return {"pin": pin, "kind": "gpio", "gpio": gpio, "name": gpio,
            "alts": alts, "adc": adc}

def _p(pin, name, kind):
    return {"pin": pin, "kind": kind, "gpio": None, "name": name,
            "alts": [], "adc": None}

# alts ordered nearest-board-first AFTER the gpio/adc box.
PINS = {
    1:  _g(1, "GP0", [("spi", "SPI0 RX"), ("i2c", "I2C0 SDA"), ("uart", "UART0 TX")]),
    2:  _g(2, "GP1", [("spi", "SPI0 CSn"), ("i2c", "I2C0 SCL"), ("uart", "UART0 RX")]),
    3:  _p(3, "GND", "ground"),
    4:  _g(4, "GP2", [("spi", "SPI0 SCK"), ("i2c", "I2C1 SDA")]),
    5:  _g(5, "GP3", [("spi", "SPI0 TX"), ("i2c", "I2C1 SCL")]),
    6:  _g(6, "GP4", [("spi", "SPI0 RX"), ("i2c", "I2C0 SDA"), ("uart", "UART1 TX")]),
    7:  _g(7, "GP5", [("spi", "SPI0 CSn"), ("i2c", "I2C0 SCL"), ("uart", "UART1 RX")]),
    8:  _p(8, "GND", "ground"),
    9:  _g(9, "GP6", [("spi", "SPI0 SCK"), ("i2c", "I2C1 SDA")]),
    10: _g(10, "GP7", [("spi", "SPI0 TX"), ("i2c", "I2C1 SCL")]),
    11: _g(11, "GP8", [("spi", "SPI1 RX"), ("i2c", "I2C0 SDA"), ("uart", "UART1 TX")]),
    12: _g(12, "GP9", [("spi", "SPI1 CSn"), ("i2c", "I2C0 SCL"), ("uart", "UART1 RX")]),
    13: _p(13, "GND", "ground"),
    14: _g(14, "GP10", [("spi", "SPI1 SCK"), ("i2c", "I2C1 SDA")]),
    15: _g(15, "GP11", [("spi", "SPI1 TX"), ("i2c", "I2C1 SCL")]),
    16: _g(16, "GP12", [("spi", "SPI1 RX"), ("i2c", "I2C0 SDA"), ("uart", "UART0 TX")]),
    17: _g(17, "GP13", [("spi", "SPI1 CSn"), ("i2c", "I2C0 SCL"), ("uart", "UART0 RX")]),
    18: _p(18, "GND", "ground"),
    19: _g(19, "GP14", [("spi", "SPI1 SCK"), ("i2c", "I2C1 SDA")]),
    20: _g(20, "GP15", [("spi", "SPI1 TX"), ("i2c", "I2C1 SCL")]),
    21: _g(21, "GP16", [("spi", "SPI0 RX"), ("i2c", "I2C0 SDA"), ("uart", "UART0 TX")]),
    22: _g(22, "GP17", [("spi", "SPI0 CSn"), ("i2c", "I2C0 SCL"), ("uart", "UART0 RX")]),
    23: _p(23, "GND", "ground"),
    24: _g(24, "GP18", [("spi", "SPI0 SCK"), ("i2c", "I2C1 SDA")]),
    25: _g(25, "GP19", [("spi", "SPI0 TX"), ("i2c", "I2C1 SCL")]),
    26: _g(26, "GP20", [("i2c", "I2C0 SDA")]),
    27: _g(27, "GP21", [("i2c", "I2C0 SCL")]),
    28: _p(28, "GND", "ground"),
    29: _g(29, "GP22", []),
    30: _p(30, "RUN", "sysctl"),
    31: _g(31, "GP26", [("i2c", "I2C1 SDA")], adc="ADC0"),
    32: _g(32, "GP27", [("i2c", "I2C1 SCL")], adc="ADC1"),
    33: _p(33, "GND", "ground"),
    34: _g(34, "GP28", [], adc="ADC2"),
    35: _p(35, "ADC_VREF", "sysctl"),
    36: _p(36, "3V3(OUT)", "power"),
    37: _p(37, "3V3_EN", "sysctl"),
    38: _p(38, "GND", "ground"),
    39: _p(39, "VSYS", "power"),
    40: _p(40, "VBUS", "power"),
}


def pod_overlay():
    """The pod's DUT-facing assignments keyed by GPIO number.

    Authoritative entries (swd/nrst/i2c_target/dut_uart) come from
    _rp2_pinmap.pinmap(); the rest (backup REPL console, ADC, logic-analyser
    default block, suggested SPI0) are fixed pod facts from hardware-setup.md,
    tagged with their VERIFIED/SUGGESTED status.
    """
    spec = importlib.util.spec_from_file_location("_rp2_pinmap", PINMAP_PY)
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    m = pm.pinmap()
    ov = {}
    ov[m["swd"]["swdio"]] = ("SWDIO", "V")
    ov[m["swd"]["swclk"]] = ("SWCLK", "V")
    ov[m["nrst"]] = ("nRST", "S")
    ov[m["i2c_target"]["sda"]] = ("I2C SDA", "V")
    ov[m["i2c_target"]["scl"]] = ("I2C SCL", "V")
    ov[m["dut_uart"]["tx"]] = ("UART TX", "S")
    ov[m["dut_uart"]["rx"]] = ("UART RX", "S")
    # Fixed pod facts not in pinmap() (hardware-setup.md):
    ov[0] = ("REPL TX", "V")     # backup UART0 REPL console (frozen)
    ov[1] = ("REPL RX", "V")
    ov[26] = ("ADC0", "V")
    ov[27] = ("ADC1", "V")
    ov[28] = ("ADC2", "V")
    for gp in (16, 17, 18, 19, 20, 21):
        ov.setdefault(gp, ("LA", "S"))
    return ov


# ── SVG geometry ─────────────────────────────────────────────────────────────
W, H = 1160, 660
PITCH = 24
BOX_H = 18
BOX_W = 66
GAP = 3
BOARD_W = 128
TOP = 96
CX = 452                       # board centre x (left of centre to leave room for legend)
BOARD_L = CX - BOARD_W // 2
BOARD_R = CX + BOARD_W // 2
BOARD_TOP = TOP - 8
BOARD_BOT = TOP + 19 * PITCH + BOX_H + 8


def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def box(parts, x, y, w, fill, text, text_fill=TEXT_LIGHT, bold=False, stroke=None):
    sw = ' stroke="%s" stroke-width="2"' % stroke if stroke else ""
    parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="3" '
                 'fill="%s"%s/>' % (x, y, w, BOX_H, fill, sw))
    fw = ' font-weight="700"' if bold else ""
    parts.append('<text x="%.1f" y="%.1f" font-family="DejaVu Sans, Arial" '
                 'font-size="10"%s fill="%s" text-anchor="middle">%s</text>'
                 % (x + w / 2, y + BOX_H - 5, fw, text_fill, esc(text)))


def pin_row(parts, entry, y, side):
    """Draw one pin row. side is 'L' (boxes extend left) or 'R' (extend right)."""
    prim_cat = "gpio" if entry["kind"] == "gpio" else entry["kind"]
    prim_fill = C.get(prim_cat, C["gpio"])
    prim_text = entry["name"]
    prim_tf = TEXT_DARK if prim_cat in ("pod",) else TEXT_LIGHT
    # box sequence outward from the board: primary (gpio/power/etc), adc, alts, pod
    seq = [(prim_fill, prim_text, prim_tf, False, None)]
    if entry.get("adc"):
        seq.append((C["adc"], entry["adc"], TEXT_LIGHT, False, None))
    for cat, txt in entry["alts"]:
        seq.append((C[cat], txt, TEXT_LIGHT, False, None))
    pod = entry.get("pod")
    if pod:
        label = pod[0] + (" [V]" if pod[1] == "V" else " [S]")
        seq.append((C["pod"], label, TEXT_DARK, True, "#ffffff"))

    # pin-number tab at the board edge
    if side == "L":
        nx = BOARD_L - 12
    else:
        nx = BOARD_R + 12
    parts.append('<circle cx="%.1f" cy="%.1f" r="8" fill="#cfd3d6"/>'
                 % (nx, y + BOX_H / 2))
    parts.append('<text x="%.1f" y="%.1f" font-family="DejaVu Sans, Arial" '
                 'font-size="9" font-weight="700" fill="#111" '
                 'text-anchor="middle">%d</text>' % (nx, y + BOX_H - 5, entry["pin"]))

    # boxes
    if side == "L":
        x = BOARD_L - 24 - BOX_W
        for fill, txt, tf, bold, stroke in seq:
            box(parts, x, y, BOX_W, fill, txt, tf, bold, stroke)
            x -= BOX_W + GAP
    else:
        x = BOARD_R + 24
        for fill, txt, tf, bold, stroke in seq:
            box(parts, x, y, BOX_W, fill, txt, tf, bold, stroke)
            x += BOX_W + GAP


def build_svg():
    ov = pod_overlay()
    for gp, ass in ov.items():
        # attach pod assignment to the matching gpio entry
        for e in PINS.values():
            if e["gpio"] == "GP%d" % gp:
                e["pod"] = ass
    p = []
    p.append('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d">' % (W, H, W, H))
    p.append('<rect width="%d" height="%d" fill="%s"/>' % (W, H, BG))
    # title
    p.append('<text x="24" y="34" font-family="DejaVu Sans, Arial" font-size="18" '
             'font-weight="700" fill="#fff">Annealage Pod - Raspberry Pi Pico 2 W pinout</text>')
    p.append('<text x="24" y="52" font-family="DejaVu Sans, Arial" font-size="11" '
             'fill="#9aa">Gold boxes = pod DUT-facing assignments. [V] verified, '
             '[S] suggested (untested). Generated from _rp2_pinmap.pinmap().</text>')

    # board
    p.append('<rect x="%d" y="%d" width="%d" height="%d" rx="12" fill="%s" '
             'stroke="%s" stroke-width="2"/>'
             % (BOARD_L, BOARD_TOP, BOARD_W, BOARD_BOT - BOARD_TOP, BOARD, BOARD_EDGE))
    # USB notch
    p.append('<rect x="%d" y="%d" width="34" height="16" rx="3" fill="#c8ccd0"/>'
             % (CX - 17, BOARD_TOP - 8))
    # RP2350 chip
    p.append('<rect x="%d" y="%d" width="56" height="56" rx="4" fill="#0c0c0c"/>'
             % (CX - 28, (BOARD_TOP + BOARD_BOT) / 2 - 28))
    p.append('<text x="%d" y="%d" font-family="DejaVu Sans, Arial" font-size="9" '
             'fill="#666" text-anchor="middle">RP2350</text>'
             % (CX, (BOARD_TOP + BOARD_BOT) / 2 + 2))
    # board name
    p.append('<text x="%d" y="%d" font-family="DejaVu Sans, Arial" font-size="10" '
             'fill="#bfe6c9" text-anchor="middle" transform="rotate(-90 %d %d)">'
             'Raspberry Pi Pico 2 W</text>'
             % (BOARD_L + 14, (BOARD_TOP + BOARD_BOT) / 2, BOARD_L + 14,
                (BOARD_TOP + BOARD_BOT) / 2))
    # SWD tab (bottom) - the pod's SWD debug pins are GP14/15 on the header, but
    # the board's own 3-pin DEBUG connector is separate; label it for orientation.
    p.append('<text x="%d" y="%d" font-family="DejaVu Sans, Arial" font-size="8" '
             'fill="#8fae98" text-anchor="middle">DEBUG</text>'
             % (CX, BOARD_BOT - 12))

    # pin rows: left = pins 1..20 top->bottom; right = pins 40..21 top->bottom
    for i in range(20):
        y = TOP + i * PITCH
        pin_row(p, PINS[i + 1], y, "L")
    for j in range(20):
        y = TOP + j * PITCH
        pin_row(p, PINS[40 - j], y, "R")

    # legend
    lx, ly = W - 218, 96
    p.append('<text x="%d" y="%d" font-family="DejaVu Sans, Arial" font-size="12" '
             'font-weight="700" fill="#fff">Legend</text>' % (lx, ly - 8))
    legend = [
        ("pod", "Annealage Pod assignment"),
        ("gpio", "GPIO / PIO / PWM"),
        ("adc", "ADC"),
        ("uart", "UART"),
        ("spi", "SPI"),
        ("i2c", "I2C"),
        ("power", "Power"),
        ("ground", "Ground"),
        ("sysctl", "System control"),
    ]
    for k, (cat, name) in enumerate(legend):
        yy = ly + k * 22
        p.append('<rect x="%d" y="%d" width="26" height="14" rx="3" fill="%s"/>'
                 % (lx, yy, C[cat]))
        p.append('<text x="%d" y="%d" font-family="DejaVu Sans, Arial" '
                 'font-size="11" fill="#dfe3e6">%s</text>' % (lx + 34, yy + 12, name))

    p.append('</svg>')
    return "\n".join(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--svg", default=os.path.join(REPO, "docs", "pod", "pinout.svg"))
    ap.add_argument("--png", default=os.path.join(REPO, "docs", "pod", "pinout.png"))
    args = ap.parse_args()
    svg = build_svg()
    with open(args.svg, "w") as f:
        f.write(svg)
    print("wrote", args.svg)
    try:
        import cairosvg
        cairosvg.svg2png(bytestring=svg.encode(), write_to=args.png, scale=2.0)
        print("wrote", args.png)
    except Exception as e:
        print("PNG skipped (%s)" % e, file=sys.stderr)


if __name__ == "__main__":
    main()
