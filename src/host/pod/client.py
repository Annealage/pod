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

import subprocess as _subprocess
from typing import Callable, List, Optional


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

    # ── stubbed methods (pending future phases) ──────────────────────────

    def flash_dut(self, image: str, target: Optional[str] = None) -> None:
        """Flash a firmware image to the DUT via the on-pod FLM loader.

        Pending Phase 2/3: requires on-pod FLM flash loader (workstream D2/D3).
        """
        raise NotImplementedError(
            "flash_dut is not yet implemented - pending Phase 2/3 (on-pod FLM loader)"
        )

    def reset_dut(self, mode: str = "swd") -> None:
        """Reset the DUT via the on-pod debug probe.

        Pending Phase 2/3: requires on-pod reset control (workstream D2/D3).
        mode: 'swd', 'nrst', or 'power' (power requires custom carrier hardware).
        """
        raise NotImplementedError(
            "reset_dut is not yet implemented - pending Phase 2/3 (on-pod reset control)"
        )

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
