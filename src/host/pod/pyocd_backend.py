"""Debug backend for a probe attached to the host, driven through pyOCD.

Used for dev boards with a built-in programmer (ST-LINK, CMSIS-DAP, ...). The
target and its flash algorithms come from the shared CMSIS pack cache owned by
:mod:`pod.cmsis_pack`; pyOCD's own pack index is never consulted.

Each operation opens a session, does its work and closes it, so the probe is
free between calls. Sessions never resume the core on close, so a halt holds
across calls until resume_dut, as it does on a pod.
"""

import binascii
import os
import socket
import time
from typing import Callable, Optional

from pod import cmsis_pack, locks
from pod.backend import DebugBackend, check_write_protect

REG_NAMES = ("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10",
             "r11", "r12", "sp", "lr", "pc", "xpsr", "msp", "psp")

_MAX_MEM = 4096
_ELF_MAGIC = b"\x7fELF"


class PyocdError(RuntimeError):
    """The pyOCD backend could not do what was asked."""


def _regname(reg) -> str:
    if isinstance(reg, str) and reg.strip().lower() in REG_NAMES:
        return reg.strip().lower()
    try:
        idx = int(reg, 0) if isinstance(reg, str) else int(reg)
    except ValueError:
        raise ValueError("unknown register %r (use 0..%d or %s)"
                         % (reg, len(REG_NAMES) - 1, "/".join(REG_NAMES)))
    if not 0 <= idx < len(REG_NAMES):
        raise ValueError("regsel out of range 0..%d" % (len(REG_NAMES) - 1))
    return REG_NAMES[idx]


def _load_pyocd():
    try:
        from pyocd.core.helpers import ConnectHelper
        from pyocd.core.target import Target
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise PyocdError("pyocd is not installed: %s" % exc) from exc
    return ConnectHelper, Target


class PyocdBackend(DebugBackend):
    """Debug ops against a host-attached probe selected by unique id."""

    backend_name = "pyocd"

    def __init__(self, uid: str, target_family: Optional[str] = None,
                 flash_base: Optional[int] = None,
                 flash_algorithm: Optional[str] = None,
                 caller: Optional[str] = None, pack: Optional[str] = None):
        self.uid = uid
        self.target_family = target_family
        self.flash_base = flash_base
        self.flash_algorithm = flash_algorithm
        self.pack_path = pack
        if caller is None:
            from pod.client import resolve_caller
            caller = resolve_caller()
        self.caller = caller

    @classmethod
    def from_entry(cls, entry: dict) -> "PyocdBackend":
        from pod.routes import route
        r = route(entry, "debug")
        if not r.get("uid"):
            raise PyocdError("debug route has no probe 'uid'")
        dut = entry.get("dut") or {}
        fb = dut.get("flash_base")
        if isinstance(fb, str):
            fb = int(fb, 0)
        return cls(r["uid"], dut.get("target_family"), fb,
                   dut.get("flash_algorithm"), pack=dut.get("pack"))

    # -- session handling ---------------------------------------------------

    def _check_algorithm(self):
        """Refuse a declared flash algorithm pyOCD would not pick.

        pyOCD loads only a pack's default algorithm per flash range and has no
        way to choose another by name, so a declared algorithm that is not the
        one the pack defaults to at flash_base cannot be honoured here. Failing
        is better than flashing with a different algorithm than declared.
        """
        if not (self.flash_algorithm and self.target_family):
            return
        pack, info = cmsis_pack.find_device(self.target_family)
        try:
            default = cmsis_pack.algorithm_name(
                info.flash_algorithm(self.flash_base))
        finally:
            pack.close()
        if default != self.flash_algorithm:
            raise PyocdError(
                "declared flash_algorithm %r is not the pack default at 0x%08x "
                "(%r); pyOCD cannot select it, so this bench cannot flash it"
                % (self.flash_algorithm, self.flash_base or 0, default))

    def _resolve_pack(self):
        """The pack file and CMSIS device name to load, or (None, None) to let
        pyOCD use a built-in target when no family is declared."""
        if not self.target_family:
            return None, None
        if self.pack_path:
            return str(self.pack_path), self.target_family
        pack, _info = cmsis_pack.find_device(self.target_family)
        return str(pack.path), self.target_family

    def _open(self, force: bool = False, **options):
        ConnectHelper, _ = _load_pyocd()
        pack, family = self._resolve_pack()
        kwargs = {"pack": pack, "target_override": family.lower()} if pack else {}
        opts = {"cmsis_pack_manager": False, "resume_on_disconnect": False,
                "connect_mode": "attach", "halt_on_connect": False}
        opts.update(options)
        session = ConnectHelper.session_with_chosen_probe(
            unique_id=self.uid, options=opts, auto_open=False,
            blocking=False, **kwargs)
        if session is None:
            raise PyocdError("no probe with unique id %s is attached" % self.uid)
        session.open()
        self._pack_used = pack
        return session

    def _session(self, force: bool = False, keep: str = "auto", **options):
        return _Session(self, force, options, keep)

    def _stamp(self, result: dict) -> dict:
        result["backend"] = self.backend_name
        if getattr(self, "_pack_used", None):
            result["pack"] = os.path.basename(self._pack_used)
        return result

    def _failed(self, exc) -> dict:
        return self._stamp({"ok": False, "err": repr(exc)})

    # -- identity -----------------------------------------------------------

    def discover_dut(self) -> dict:
        try:
            with self._session() as s:
                t = s.target
                ap = t.aps[min(t.aps)] if t.aps else None
                return self._stamp({
                    "ok": True,
                    "dpidr": int(t.dp.dpidr.idr),
                    "ap_idr": int(ap.idr) if ap else None,
                    "cpuid": t.read32(0xE000ED00),
                    "rom_base": int(ap.rom_addr) if ap else None,
                })
        except Exception as exc:  # noqa: BLE001 - reported as a result
            return self._failed(exc)

    # -- run control --------------------------------------------------------

    def halt_dut(self, keep_attached: bool = False, force: bool = False) -> dict:
        try:
            with self._session(force, keep="hold") as s:
                s.target.halt()
                return self._stamp({
                    "ok": True, "halted": True,
                    "dhcsr": s.target.read32(0xE000EDF0)})
        except locks_conflict():
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def resume_dut(self) -> dict:
        try:
            with self._session(keep="release") as s:
                s.target.resume()
                return self._stamp({"ok": True, "halted": False})
        except locks_conflict():
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def reset_dut(self, mode: str = "sysreset", keep_attached: bool = False,
                  force: bool = False) -> dict:
        if mode not in ("sysreset", "halt", "nrst"):
            raise ValueError("reset mode %r unknown (sysreset, halt, nrst)" % mode)
        try:
            with self._session(force, keep="hold" if mode == "halt" else "release") as s:
                if mode == "nrst":
                    s.session.probe.assert_reset(True)
                    time.sleep(0.05)
                    s.session.probe.assert_reset(False)
                elif mode == "halt":
                    s.target.reset_and_halt()
                else:
                    s.target.reset()
                return self._stamp({"ok": True, "mode": mode})
        except locks_conflict():
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    # -- registers and memory ----------------------------------------------

    def _halted(self, s) -> bool:
        _, Target = _load_pyocd()
        return s.target.get_state() == Target.State.HALTED

    def read_reg(self, reg) -> dict:
        name = _regname(reg)
        try:
            with self._session() as s:
                if not self._halted(s):
                    return self._stamp({"ok": False, "err": "core is running; "
                                        "halt it first (registers need a halted core)"})
                return self._stamp({
                    "ok": True, "regsel": REG_NAMES.index(name),
                    "value": s.target.read_core_register_raw(name) & 0xFFFFFFFF})
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def write_reg(self, reg, value: int) -> dict:
        name = _regname(reg)
        value &= 0xFFFFFFFF
        try:
            with self._session() as s:
                if not self._halted(s):
                    return self._stamp({"ok": False, "err": "core is running; "
                                        "halt it first (registers need a halted core)"})
                s.target.write_core_register_raw(name, value)
                return self._stamp({"ok": True, "regsel": REG_NAMES.index(name),
                                    "value": value})
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def read_mem(self, addr: int, length: int) -> dict:
        if not 0 <= length <= _MAX_MEM:
            raise ValueError("length %d out of range 0..%d (use read_dut for bulk)"
                             % (length, _MAX_MEM))
        try:
            with self._session() as s:
                data = bytes(s.target.read_memory_block8(addr, length))
                return self._stamp({"ok": True, "addr": addr, "length": length,
                                    "hex": binascii.hexlify(data).decode()})
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def write_mem(self, addr: int, data, protect=None) -> dict:
        data_hex = data if isinstance(data, str) else binascii.hexlify(bytes(data)).decode()
        raw = binascii.unhexlify(data_hex)
        if len(raw) > _MAX_MEM:
            raise ValueError("data %d bytes exceeds %d" % (len(raw), _MAX_MEM))
        refused = check_write_protect(addr, len(raw), protect)
        if refused:
            return self._stamp(refused)
        try:
            with self._session() as s:
                s.target.write_memory_block8(addr, list(raw))
                return self._stamp({"ok": True, "addr": addr, "length": len(raw)})
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def read_dut(self, addr: int, length: int, out_path: str,
                 port: int = 3334) -> str:
        with self._session() as s, open(out_path, "wb") as f:
            off = 0
            while off < length:
                n = min(_MAX_MEM * 16, length - off)
                f.write(bytes(s.target.read_memory_block8(addr + off, n)))
                off += n
        return "Read %d bytes from 0x%08x to %s" % (length, addr, out_path)

    # -- flash --------------------------------------------------------------

    def erase_dut(self, clkdiv: int = 0, keep_attached: bool = False,
                  force: bool = False) -> dict:
        t0 = time.time()
        try:
            self._check_algorithm()
            with self._session(force, keep="release", connect_mode="halt") as s:
                from pyocd.flash.eraser import FlashEraser
                FlashEraser(s.session, FlashEraser.Mode.CHIP).erase()
                return self._stamp({"ok": True, "ms": int((time.time() - t0) * 1000),
                                    "err": None})
        except locks_conflict():
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True, port: int = 3333,
                  keep_attached: bool = False, mass_erase: bool = False,
                  force: bool = False) -> dict:
        if target and target != self.target_family:
            self.target_family = target
        with open(image, "rb") as f:
            is_elf = f.read(4) == _ELF_MAGIC
        base = addr or self.flash_base
        if not is_elf and base is None:
            raise ValueError("a flat binary needs addr or a declared dut flash_base")
        t0 = time.time()
        try:
            self._check_algorithm()
            with self._session(force, keep="release", connect_mode="halt") as s:
                from pyocd.flash.file_programmer import FileProgrammer
                progr = FileProgrammer(
                    s.session, progress=lambda fraction: None,
                    chip_erase="chip" if mass_erase else "sector",
                    smart_flash=True)
                if is_elf:
                    progr.program(image, file_format="elf")
                else:
                    progr.program(image, file_format="bin", base_address=base)
                result = {"ok": True, "bytes": os.path.getsize(image),
                          "err": None}
                if not is_elf:
                    result["addr"] = base
                if verify:
                    err = self._verify(s, image, is_elf, base)
                    if err:
                        result.update(ok=False, err=err)
                s.target.reset()
                result["ms"] = int((time.time() - t0) * 1000)
                return self._stamp(result)
        except locks_conflict():
            raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(exc)

    @staticmethod
    def _verify(s, image, is_elf, base) -> Optional[str]:
        """Read the programmed flash back and compare; return an error or None."""
        if is_elf:
            from pyocd.debug.elf.elf import ELFBinaryFile
            regions = [(sec.start, bytes(sec.data)) for sec in
                       ELFBinaryFile(image).sections if sec.length]
            regions = [(a, d) for a, d in regions
                       if s.target.memory_map.get_region_for_address(a)
                       and s.target.memory_map.get_region_for_address(a).is_flash]
        else:
            with open(image, "rb") as f:
                regions = [(base, f.read())]
        for a, want in regions:
            got = bytes(s.target.read_memory_block8(a, len(want)))
            if got != want:
                at = next(i for i in range(len(want)) if got[i] != want[i])
                return "verify mismatch at 0x%08x" % (a + at)
        return None

    # -- gdb ----------------------------------------------------------------

    def gdb_endpoint(self, listen_port: int = 0, gdb_port: int = 3335,
                     reset_halt: bool = True, resume_window_ms: int = 200,
                     on_listen: Optional[Callable] = None) -> tuple:
        from pyocd.gdbserver import GDBServer
        if not listen_port:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                listen_port = sock.getsockname()[1]
        with self._session(keep="release",
                           connect_mode="halt" if reset_halt else "attach",
                           gdbserver_port=listen_port,
                           persist=True) as s:
            server = GDBServer(s.session, core=0)
            server.start()
            if on_listen:
                on_listen("127.0.0.1", listen_port)
            try:
                while server.is_alive():
                    server.join(0.5)
            except KeyboardInterrupt:
                server.stop()
        return ("127.0.0.1", listen_port)


def locks_conflict():
    from pod.client import PodConflictError
    return PodConflictError


# Sessions kept open across calls, keyed by probe uid. Some probes (ST-LINK)
# reset the target when a session closes, which would undo a halt; a halted
# core therefore keeps its session, and the probe lock, until it is resumed,
# reset, flashed or erased. Held per process, so a one-shot CLI call cannot
# leave a core halted on such a probe.
_HELD: dict = {}


class _Session:
    """A locked, open pyOCD session for one operation.

    keep: "auto" reuses a held session if there is one and otherwise closes on
    exit; "hold" leaves the session open for later calls; "release" closes a
    held session on exit.
    """

    def __init__(self, backend: PyocdBackend, force: bool, options: dict,
                 keep: str = "auto"):
        self._backend = backend
        self._force = force
        self._options = options
        self._keep = keep

    def __enter__(self):
        held = _HELD.get(self._backend.uid)
        if held:
            self._lock, self.session = held
            self._reused = True
        else:
            self._reused = False
            self._lock = locks.hold("pyocd-" + self._backend.uid,
                                    self._backend.caller, self._force)
            self._lock.__enter__()
            try:
                self.session = self._backend._open(self._force, **self._options)
            except BaseException:
                self._lock.__exit__(None, None, None)
                raise
        self.target = self.session.target
        return self

    def __exit__(self, exc_type, *rest):
        uid = self._backend.uid
        keep_open = (self._keep == "hold" and exc_type is None) or (
            self._reused and self._keep == "auto")
        if keep_open:
            _HELD[uid] = (self._lock, self.session)
            return
        _HELD.pop(uid, None)
        try:
            self.session.close()
        finally:
            self._lock.__exit__(None, None, None)
