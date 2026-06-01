"""Pod control client built on ampremote.

One Pod instance per pod. All transport goes through ampremote using the
socket://HOST:PORT connect form; no soft reset is performed by default.

The subprocess runner is injected (default subprocess.run) so tests can
pass a fake without invoking the real ampremote.

Stubbed methods raise NotImplementedError with the phase they're pending:
  flash_dut, reset_dut  - pending Phase 2/3 (on-pod FLM loader / GDB)
  usbip_attach          - pending Phase 4 (DUT USB host + USB/IP)
  uart_stream           - pending Phase 5 (UART-over-TCP)
  telemetry             - pending Phase 5 (INA228; custom carrier hardware)
  gdb_endpoint          - pending Phase 3 (on-pod GDB server)
"""

import ast
import subprocess as _subprocess
from typing import Callable, List, Optional


def _last_dict(stdout: str) -> dict:
    """Parse the last printed dict literal from on-pod stdout."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return ast.literal_eval(line)
            except (ValueError, SyntaxError):
                pass
    return {"raw": stdout}


class Pod:
    """Control client for a single Annealage Pod over ampremote socket transport."""

    def __init__(
        self,
        address: str,
        repl_port: int = 8266,
        runner: Optional[Callable] = None,
    ):
        """Create a Pod client.

        Args:
            address: IPv4 address of the pod.
            repl_port: TCP port of the ampremote socket REPL.
            runner: Callable with the same signature as subprocess.run.
                    Defaults to subprocess.run. Inject a fake for testing.
        """
        self.address = address
        self.repl_port = repl_port
        self._runner = runner if runner is not None else _subprocess.run

    # ── argv construction (pure, testable) ───────────────────────────────

    def _argv(self, verb: str, *args: str) -> List[str]:
        """Build the ampremote argv list for a given verb and arguments.

        Returns a list starting with ['ampremote', 'connect',
        'socket://ADDRESS:PORT', verb, *args].
        No subprocess is invoked.
        """
        connect_target = f"socket://{self.address}:{self.repl_port}"
        return ["ampremote", "connect", connect_target, verb] + list(args)

    # ── live commands ────────────────────────────────────────────────────

    def exec(self, code: str) -> str:
        """Execute a MicroPython code string on the pod and return stdout.

        Uses ampremote exec verb. Raises subprocess.CalledProcessError on
        non-zero exit.
        """
        argv = self._argv("exec", code)
        result = self._runner(argv, capture_output=True, text=True, check=True)
        return result.stdout

    def eval(self, expr: str) -> str:
        """Evaluate a MicroPython expression on the pod and return the result string."""
        argv = self._argv("eval", expr)
        result = self._runner(argv, capture_output=True, text=True, check=True)
        return result.stdout

    def cp(self, src: str, dst: str) -> None:
        """Copy a file to or from the pod.

        Use ':path' prefix for pod-side paths, per ampremote convention.
        """
        argv = self._argv("fs", "cp", src, dst)
        self._runner(argv, check=True)

    def mount(self, directory: str) -> None:
        """Mount a local host directory on the pod over ampremote.

        Runs ampremote in the foreground; blocks until the user disconnects.
        """
        argv = self._argv("mount", directory)
        self._runner(argv)

    def repl(self) -> None:
        """Attach an interactive REPL to the pod. Blocks until disconnected."""
        argv = self._argv("repl")
        self._runner(argv)

    # ── DUT flash / reset / read (on-pod debug stack, workstream D) ───────

    def flash_dut(self, image: str, target: Optional[str] = None,
                  addr: int = 0, verify: bool = True) -> dict:
        """Flash a firmware image file to the DUT over SWD via the on-pod loader.

        The image is copied to the pod and programmed + verified in bounded
        chunks on the pod (it is never held whole in pod RAM, and the prior DUT
        contents are not read). Returns the on-pod result dict
        {ok, addr, bytes, ms}. `target` is reserved for selecting target data
        once more than the nRF52 native path exists.
        """
        remote = "_dutimg.bin"
        self.cp(image, ":" + remote)
        try:
            out = self.exec(
                "import annealage_pod.debug.ops as o;"
                "print(o.flash_file(%d, %r, verify=%s))"
                % (addr, remote, bool(verify))
            )
        finally:
            try:
                self.exec("import os; os.remove(%r)" % remote)
            except Exception:
                pass
        return _last_dict(out)

    def reset_dut(self, mode: str = "sysreset") -> dict:
        """Reset the DUT via the on-pod debug probe.

        mode: 'sysreset' (reset and run) or 'halt' (reset and halt at the
        vector). nRST and power-cycle reset need carrier hardware not present.
        """
        out = self.exec(
            "import annealage_pod.debug.ops as o; print(o.reset(%r))" % mode
        )
        return _last_dict(out)

    def read_dut(self, addr: int, length: int, out_path: str) -> str:
        """Explicitly read `length` bytes of DUT flash from `addr` to a host file.

        This is the only path that returns target contents, and only when
        called; flashing never reads the DUT back to the host.
        """
        remote = "_dump.bin"
        self.exec(
            "import annealage_pod.debug.ops as o;"
            "print(o.dump_to_file(%d, %d, %r))" % (addr, length, remote)
        )
        self.cp(":" + remote, out_path)
        try:
            self.exec("import os; os.remove(%r)" % remote)
        except Exception:
            pass
        return out_path

    # ── stubbed methods (pending future phases) ──────────────────────────

    def usbip_attach(self) -> None:
        """Attach the DUT USB device over USB/IP.

        Pending Phase 4: requires DUT USB host + USB/IP server on the pod.
        """
        raise NotImplementedError(
            "usbip_attach is not yet implemented - pending Phase 4 (DUT USB host + USB/IP)"
        )

    def uart_stream(self) -> None:
        """Stream DUT UART output over TCP.

        Pending Phase 5: requires UART-over-TCP on the pod.
        """
        raise NotImplementedError(
            "uart_stream is not yet implemented - pending Phase 5 (UART-over-TCP)"
        )

    def telemetry(self) -> None:
        """Read INA228 power telemetry from the pod carrier.

        Pending Phase 5: requires INA228 driver and custom carrier hardware.
        """
        raise NotImplementedError(
            "telemetry is not yet implemented - pending Phase 5 (INA228 telemetry, custom carrier)"
        )

    def gdb_endpoint(self) -> None:
        """Return connection details for the on-pod GDB server.

        Pending Phase 3: requires on-pod GDB server (workstream D3).
        """
        raise NotImplementedError(
            "gdb_endpoint is not yet implemented - pending Phase 3 (on-pod GDB server)"
        )
