# PIO logic analyser for the Annealage Pod (RP2350), workstream E / Track 2.
#
# Samples `width` contiguous GPIOs once per PIO clock and DMA-streams packed
# 32-bit words into a RAM ring; an optional trigger gates the start. The capture
# program and sampling were validated on the rig (a 1 kHz PWM recovered exactly
# at 100 kHz polled and 1 MHz DMA); see docs/rp2350/logic-analyser.md.
#
# PIO budget: the LA runs on PIO2 (SM10 by default; PIO0=Wi-Fi, PIO1=SWD). The
# `ops` layer swaps SWD out via the PIO arbiter before a capture so the two are
# never live at once, per the settled design.
#
# DMA register facts, validated on this silicon (RP2350):
#   PIO block base = 0x50200000 + block*0x100000   (PIO0/1/2)
#   RX FIFO<sm>    = base + 0x20 + sm*4
#   RX DREQ<sm>    = block*8 + 4 + sm
# (block = sm_id // 4, sm = sm_id % 4.)

import array
import time

import rp2
from machine import Pin

SYS_HZ = 150_000_000

# Trigger conditions on a single pin.
_TRIGGERS = ("rise", "fall", "high", "low")


def _pio_base(block):
    return 0x50200000 + block * 0x100000


def rx_fifo_addr(sm_id):
    return _pio_base(sm_id // 4) + 0x20 + (sm_id % 4) * 4


def rx_dreq(sm_id):
    return (sm_id // 4) * 8 + 4 + (sm_id % 4)


def _build_prog(width, trigger):
    """Assemble a capture program for `width` pins and an optional trigger.

    `trigger` is None or (pin, cond) with cond in _TRIGGERS. The Python `if`s run
    at assembly time (width/trigger are constants), so each config compiles to
    just the instructions it needs.
    """
    w = width
    trig = trigger

    @rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT, autopush=True, push_thresh=32)
    def prog():
        if trig is not None:
            tpin, cond = trig
            # Pre-roll: block until the trigger condition, then fall into the
            # free-running sample loop.
            if cond == "rise":
                wait(0, gpio, tpin)
                wait(1, gpio, tpin)
            elif cond == "fall":
                wait(1, gpio, tpin)
                wait(0, gpio, tpin)
            elif cond == "high":
                wait(1, gpio, tpin)
            else:  # low
                wait(0, gpio, tpin)
        wrap_target()
        in_(pins, w)
        wrap()

    return prog


class LogicAnalyser:
    """A PIO logic analyser on one PIO state machine, DMA into a RAM buffer.

    base_pin: lowest GPIO sampled; width: number of contiguous GPIOs (1..32).
    sm_id 8..11 are PIO2 (10 is the validated default; 8 is externally claimed).
    """

    # Cap the capture buffer so a request cannot exhaust pod RAM.
    MAX_WORDS = 20000   # 80 KB

    def __init__(self, base_pin, width=1, sm_id=10):
        if not (1 <= width <= 32):
            raise ValueError("width must be 1..32")
        self.base_pin = base_pin
        self.width = width
        self.sm_id = sm_id
        self.sm = None
        self.dma = None
        self.buf = None

    def _teardown(self):
        if self.dma is not None:
            try:
                self.dma.active(0)
                self.dma.close()
            except Exception:
                pass
            self.dma = None
        if self.sm is not None:
            try:
                self.sm.active(0)
            except Exception:
                pass
            self.sm = None
        # free this PIO block's instruction memory so repeated captures (and a
        # later SWD rebuild, if the LA ever used PIO1) do not leak it
        try:
            rp2.PIO(self.sm_id // 4).remove_program()
        except Exception:
            pass

    def release(self):
        self._teardown()
        self.buf = None

    def capture(self, rate, depth, trigger=None):
        """Capture `depth` samples at ~`rate` Hz; return a result dict.

        trigger: None (start immediately) or (pin, cond), cond in
        ('rise','fall','high','low'). Returns:
          {ok, width, base_pin, rate (actual Hz), depth, words,
           samples_per_word, complete, buf (array 'I')}
        `complete` is False if a trigger never fired (capture timed out).
        """
        if trigger is not None:
            tpin, cond = trigger
            if cond not in _TRIGGERS:
                raise ValueError("trigger cond must be one of %s" % (_TRIGGERS,))

        clkdiv = max(1, min(65535, round(SYS_HZ / rate)))
        actual = SYS_HZ / clkdiv
        words = (depth * self.width + 31) // 32
        if words > self.MAX_WORDS:
            words = self.MAX_WORDS
        depth_actual = words * 32 // self.width

        self._teardown()
        prog = _build_prog(self.width, trigger)
        self.sm = rp2.StateMachine(
            self.sm_id, prog, freq=int(actual), in_base=Pin(self.base_pin))
        self.buf = array.array("I", bytes(4 * words))

        use_dma = hasattr(rp2, "DMA")
        if use_dma:
            self.dma = rp2.DMA()
            ctrl = self.dma.pack_ctrl(
                size=2, inc_read=False, inc_write=True, treq_sel=rx_dreq(self.sm_id))
            self.dma.config(read=rx_fifo_addr(self.sm_id), write=self.buf,
                            count=words, ctrl=ctrl, trigger=False)
            self.dma.active(1)
            self.sm.active(1)
            # bound the wait: capture time + slack (a trigger may never fire)
            budget = int(words * 32 / self.width / actual * 1000) + 3000
            t0 = time.ticks_ms()
            while self.dma.active() and time.ticks_diff(time.ticks_ms(), t0) < budget:
                pass
            complete = not self.dma.active()
        else:
            # Polled fallback: rate-limited, fine for slow signals.
            self.sm.active(1)
            mv = self.buf
            i = 0
            budget = int(words * 32 / self.width / actual * 1000) + 3000
            t0 = time.ticks_ms()
            while i < words and time.ticks_diff(time.ticks_ms(), t0) < budget:
                mv[i] = self.sm.get()
                i += 1
            complete = i >= words

        buf = self.buf
        self._teardown()
        return {
            "ok": True, "width": self.width, "base_pin": self.base_pin,
            "rate": actual, "clkdiv": clkdiv, "depth": depth_actual, "words": words,
            "samples_per_word": 32 // self.width if self.width in (1, 2, 4, 8, 16, 32) else None,
            "dma": use_dma, "complete": complete, "buf": buf,
        }
