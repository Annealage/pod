"""Debug backends: how the ``dut_*`` operations reach the DUT's debug port.

``Pod`` is the pod backend (the on-pod SWD stack, driven over the socket REPL).
Other backends implement the same operations against a probe attached to the
host. :func:`debug_backend` picks one from an entry's ``debug`` route.
"""

from abc import ABC, abstractmethod
from typing import Callable, Optional

from pod.routes import route, RouteError


class DebugBackend(ABC):
    """The debug operations a bench must provide, whatever carries them.

    Every result dict carries ``backend`` naming the implementation, so a flash
    failure is attributable to the path that ran it.
    """

    backend_name: str = ""

    @abstractmethod
    def discover_dut(self) -> dict: ...

    @abstractmethod
    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True, port: int = 3333,
                  keep_attached: bool = False, mass_erase: bool = False,
                  force: bool = False) -> dict: ...

    @abstractmethod
    def erase_dut(self, clkdiv: int = 0, keep_attached: bool = False,
                  force: bool = False) -> dict: ...

    @abstractmethod
    def reset_dut(self, mode: str = "sysreset", keep_attached: bool = False,
                  force: bool = False) -> dict: ...

    @abstractmethod
    def halt_dut(self, keep_attached: bool = False,
                 force: bool = False) -> dict: ...

    @abstractmethod
    def resume_dut(self) -> dict: ...

    @abstractmethod
    def read_reg(self, reg) -> dict: ...

    @abstractmethod
    def write_reg(self, reg, value: int) -> dict: ...

    @abstractmethod
    def read_mem(self, addr: int, length: int) -> dict: ...

    @abstractmethod
    def write_mem(self, addr: int, data, protect=None) -> dict: ...

    @abstractmethod
    def read_dut(self, addr: int, length: int, out_path: str,
                 port: int = 3334) -> str: ...

    @abstractmethod
    def gdb_endpoint(self, listen_port: int = 0, gdb_port: int = 3335,
                     reset_halt: bool = True, resume_window_ms: int = 200,
                     on_listen: Optional[Callable] = None) -> tuple: ...


def check_write_protect(addr: int, nbytes: int, protect) -> Optional[dict]:
    """A refusal result when [addr, addr+nbytes) overlaps a protected range."""
    end = addr + nbytes
    for lo, hi in (protect or []):
        if addr < hi and end > lo:
            return {"ok": False, "addr": addr,
                    "err": "addr 0x%08x..0x%08x overlaps write-protected "
                    "0x%08x..0x%08x" % (addr, end, int(lo), int(hi))}
    return None


def debug_backend(entry: dict) -> DebugBackend:
    """The debug backend for an entry, chosen by its ``debug`` route."""
    via = route(entry, "debug")["via"]
    if via == "pod":
        from pod.client import Pod
        return Pod.from_entry(entry)
    if via == "pyocd":
        from pod.pyocd_backend import PyocdBackend
        return PyocdBackend.from_entry(entry)
    raise RouteError("debug route %r has no backend" % via)
