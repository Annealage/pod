"""Routes, host locks and local-bench registration."""

import json

import pytest

from pod import local, locks
from pod.backend import debug_backend
from pod.client import LocalBenchError, Pod, PodConflictError
from pod.pyocd_backend import PyocdBackend, _regname
from pod.routes import RouteError, has_pod_handles, instruments_pod, route

POD_ENTRY = {"hostname": "p.local", "addr4": "10.0.0.2",
             "dut": {"usb": {"conn": "agent-direct"}}}


class TestRoutes:
    def test_undeclared_channels_default_to_pod(self):
        for channel in ("debug", "uart", "instruments"):
            assert route(POD_ENTRY, channel) == {"via": "pod"}

    def test_usb_route_reads_the_dut_usb_block(self):
        assert route(POD_ENTRY, "usb")["via"] == "agent-direct"

    def test_declared_route_keeps_its_fields(self):
        entry = dict(POD_ENTRY, uart={"via": "tty", "tty": "/dev/x", "baud": 9600})
        assert route(entry, "uart") == {"via": "tty", "tty": "/dev/x", "baud": 9600}

    @pytest.mark.parametrize("bad", [{"via": "carrier-pigeon"}, {"tty": "/dev/x"}, "tty"])
    def test_malformed_route_is_rejected(self, bad):
        with pytest.raises(RouteError):
            route({"uart": bad}, "uart")

    def test_instruments_pod_resolution(self):
        assert instruments_pod(POD_ENTRY, "a") == "a"
        assert instruments_pod(dict(POD_ENTRY, instruments={"via": "none"}), "a") is None
        assert instruments_pod({"instruments": {"via": "pod", "pod": "b"}}, "a") == "b"
        assert instruments_pod({}, "a") is None


class TestBackendSelection:
    def test_pod_route_builds_the_pod_backend(self):
        assert isinstance(debug_backend(POD_ENTRY), Pod)

    def test_pyocd_route_builds_pyocd_with_declared_geometry(self):
        entry = local.bench_entry("UID1", "STM32H563ZITx", flash_base=0x08000000)
        backend = debug_backend(entry)
        assert isinstance(backend, PyocdBackend)
        assert (backend.uid, backend.target_family, backend.flash_base) == (
            "UID1", "STM32H563ZITx", 0x08000000)

    def test_pyocd_route_needs_a_uid(self):
        with pytest.raises(Exception, match="uid"):
            PyocdBackend.from_entry({"debug": {"via": "pyocd"}})

    def test_pod_operation_on_a_bench_without_a_pod_is_refused(self):
        bench = local.bench_entry("UID1", "STM32H563ZITx")
        assert not has_pod_handles(bench)
        with pytest.raises(LocalBenchError):
            Pod.from_entry(bench)


class TestRegisterNames:
    def test_register_names_and_numbers_agree(self):
        assert _regname("PC") == "pc"
        assert _regname("15") == "pc"
        assert _regname(0) == "r0"

    @pytest.mark.parametrize("bad", ["r99", 19, "-1"])
    def test_unknown_register_is_refused(self, bad):
        with pytest.raises(ValueError):
            _regname(bad)


class TestHostLock:
    def test_second_holder_is_refused_naming_the_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANNEALAGE_POD_LOCK_DIR", str(tmp_path))
        with locks.hold("probe-1", "agent-a"):
            with pytest.raises(PodConflictError) as exc:
                with locks.hold("probe-1", "agent-b"):
                    pass
            assert exc.value.holder["caller"] == "agent-a"

    def test_force_proceeds_without_the_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANNEALAGE_POD_LOCK_DIR", str(tmp_path))
        with locks.hold("probe-1", "agent-a"):
            with locks.hold("probe-1", "agent-b", force=True):
                pass

    def test_lock_is_free_after_release(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANNEALAGE_POD_LOCK_DIR", str(tmp_path))
        with locks.hold("probe-1", "agent-a"):
            pass
        with locks.hold("probe-1", "agent-b"):
            pass
        assert locks.read_holder("probe-1") == {}


class TestMpyDevSeeding:
    def _registry(self, tmp_path, monkeypatch):
        f = tmp_path / "devices.json"
        f.write_text(json.dumps({
            "devices": {
                "board": {"vid": "f055", "serial_number": "B1",
                          "by_id_path": "/dev/serial/by-id/board"},
                "board-link": {"vid": "0483", "serial_number": "STL1",
                               "by_id_path": "/dev/serial/by-id/link-if02"},
            },
            "links": [{"devices": ["board", "board-link"], "rel": "stlink"}]}))
        monkeypatch.setenv("MPY_DEV_CONFIG", str(f))

    @pytest.mark.parametrize("name", ["board", "board-link"])
    def test_either_linked_label_yields_the_same_bench(self, tmp_path, monkeypatch, name):
        self._registry(tmp_path, monkeypatch)
        assert local.from_mpy_dev(name) == {
            "uid": "STL1", "uart_tty": "/dev/serial/by-id/link-if02",
            "usb_tty": "/dev/serial/by-id/board"}

    def test_device_without_a_linked_probe_is_refused(self, tmp_path, monkeypatch):
        self._registry(tmp_path, monkeypatch)
        (tmp_path / "devices.json").write_text(json.dumps(
            {"devices": {"lone": {"vid": "f055", "serial_number": "L"}}, "links": []}))
        with pytest.raises(RouteError):
            local.from_mpy_dev("lone")


class TestUartPump:
    def test_socket_stream_ends_when_the_peer_closes(self):
        import socket
        from pod import uart
        a, b = socket.socketpair()
        a.sendall(b"hello")
        a.close()
        b.settimeout(0.1)
        chunks = []
        result = uart.pump(b, on_output=chunks.append)
        assert b"".join(chunks) == b"hello"
        assert result == {"ok": True, "bytes_received": 5}

    def test_idle_serial_read_is_not_end_of_stream(self):
        from pod import uart

        class Port:
            def __init__(self):
                self.reads = [b"a", b"", b"b"]

            def read(self, n):
                return self.reads.pop(0) if self.reads else b""

            def close(self):
                pass

        chunks = []
        ticks = iter(range(100))
        import time
        real = time.monotonic
        time.monotonic = lambda: next(ticks) * 0.1
        try:
            result = uart.pump(uart._SerialConn(Port()), duration=1.0,
                               on_output=chunks.append, eof_on_empty=False)
        finally:
            time.monotonic = real
        assert b"".join(chunks) == b"ab"
        assert result["bytes_received"] == 2


class TestDirectUsb:
    def test_direct_tty_only_for_agent_direct_usb(self):
        from pod.routes import dut_direct_tty
        direct = {"dut": {"usb": {"conn": "agent-direct", "tty": "/dev/x"}}}
        forwarded = {"dut": {"usb": {"conn": "pod-host", "tty": "/dev/x"}}}
        assert dut_direct_tty(direct) == "/dev/x"
        assert dut_direct_tty(forwarded) is None
        assert dut_direct_tty({}) is None
