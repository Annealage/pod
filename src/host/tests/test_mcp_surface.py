"""Locks down the MCP/CLI surface contract (docs/pod/plan/mcp-surface.md):
exactly 28 namespaced MCP tools with one dispatch branch each, the
multiplexed tools (dut_reg, dut_mem, dut_link, bench_device) routing on
their read/write/action/bus arguments, and the CLI's nested `pod dut <verb>`
/ `pod bench <verb>` tree, with nothing outside that contract reachable on
either surface."""

import inspect
import re
import sys
from functools import lru_cache

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

import pod.mcp_server as m
from pod.cli import main


@pytest.fixture(autouse=True)
def _no_leaked_sessions():
    """The session store and the peripheral-ownership tracker are module
    globals the handlers write to directly, so a test that opens one would
    otherwise leave it visible to the next test in the file."""
    m._SESSIONS.clear()
    m._OWNED_PERIPHERALS.clear()
    yield
    m._SESSIONS.clear()
    m._OWNED_PERIPHERALS.clear()


# ── shared extraction from mcp_server.py's build_server() ──────────────────
#
# list_tools()/call_tool() are nested functions registered onto an mcp.Server
# instance inside build_server(); the only externally reachable copy of their
# names is by introspecting the source, which works whether or not the `mcp`
# package is installed (build_server() itself requires it, but its source is
# always inspectable).


@lru_cache(maxsize=None)
def _build_server_source():
    return inspect.getsource(m.build_server)


@lru_cache(maxsize=None)
def _tool_list_names():
    """Every Tool(name="...") passed to list_tools()'s return list."""
    src = _build_server_source()
    body = re.search(
        r"async def list_tools\(\):.*?(?=async def call_tool)", src, re.S).group(0)
    return tuple(re.findall(r'name="([a-z_]+)"', body))


@lru_cache(maxsize=None)
def _dispatch_branch_names():
    """Every `name == "..."` branch in call_tool()'s if/elif chain."""
    src = _build_server_source()
    body = re.search(
        r'async def call_tool\(name: str, arguments: dict\):.*?\n    return server',
        src, re.S).group(0)
    return tuple(re.findall(r'name == "([a-z_]+)"', body))


EXPECTED_TOOLS = {
    "pod_discover", "pod_register", "pod_info", "pod_exec", "pod_mount", "pod_open",
    "dut_open", "session_send", "session_read", "session_close",
    "dut_exec", "dut_identify", "dut_halt", "dut_resume", "dut_reg", "dut_mem",
    "dut_gdb", "dut_flash", "dut_erase", "dut_flm", "dut_reset", "dut_link",
    "bench_gpio", "bench_adc", "bench_la", "bench_device", "bench_device_regs",
    "bench_uart",
}

# Names that must never appear on the MCP surface. Each operation they
# would name is covered by one of the EXPECTED_TOOLS above.
REMOVED_TOOLS = {
    "discover_pods", "register_pod", "repl_list", "mount_dir", "repl_open",
    "attach_dut", "ensure_dut_link", "recover_dut_repl", "repl_send",
    "repl_interrupt", "repl_read", "repl_close", "dut", "dut_read_reg",
    "dut_write_reg", "dut_read_mem", "dut_write_mem", "read_dut", "gdb_dut",
    "flash_dut", "erase_dut", "reset_dut", "detach_dut", "dut_usb",
    "reprobe_dut", "gpio", "adc", "logic_analyse", "i2c_target", "spi_target",
    "spi_target_status", "peripheral_release", "i2c_target_regs",
    "spi_target_regs", "tail_uart",
}


class TestMcpToolListExact:
    """The tool list is exactly the 28 named tools, no more and no fewer."""

    def test_tool_list_has_no_duplicates(self):
        names = _tool_list_names()
        assert len(names) == len(set(names))

    def test_tool_list_is_exactly_the_28_named_tools(self):
        assert set(_tool_list_names()) == EXPECTED_TOOLS
        assert len(_tool_list_names()) == 28


class TestMcpDispatchParity:
    """Every listed tool has a dispatch branch, and every dispatch branch is
    a listed tool - no orphan either way."""

    def test_dispatch_branches_have_no_duplicates(self):
        branches = _dispatch_branch_names()
        assert len(branches) == len(set(branches))

    def test_every_tool_has_a_dispatch_branch(self):
        missing = set(_tool_list_names()) - set(_dispatch_branch_names())
        assert missing == set()

    def test_every_dispatch_branch_is_a_listed_tool(self):
        orphans = set(_dispatch_branch_names()) - set(_tool_list_names())
        assert orphans == set()


class TestMcpNoOldToolNamesReachable:
    def test_no_removed_tool_name_in_the_list(self):
        assert set(_tool_list_names()) & REMOVED_TOOLS == set()

    def test_no_removed_tool_name_in_the_dispatcher(self):
        assert set(_dispatch_branch_names()) & REMOVED_TOOLS == set()


# ── merged-tool routing: dut_reg, dut_mem, dut_link, bench_device ──────────


class TestMcpDutRegRouting:
    def _patch(self, monkeypatch, fake_pod):
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry", classmethod(lambda cls, e: fake_pod))

    def test_value_omitted_reads(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.read_reg.return_value = {"ok": True, "regsel": 0, "value": 0x1000}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_reg("lab", "r0")
        fake_pod.read_reg.assert_called_once_with("r0")
        fake_pod.write_reg.assert_not_called()
        assert result["value"] == 0x1000

    def test_value_given_writes(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.write_reg.return_value = {"ok": True, "regsel": 0, "value": 0x2000}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_reg("lab", "r0", value=0x2000)
        fake_pod.write_reg.assert_called_once_with("r0", 0x2000)
        fake_pod.read_reg.assert_not_called()
        assert result["value"] == 0x2000


class TestMcpDutMemRouting:
    def _patch(self, monkeypatch, fake_pod):
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry", classmethod(lambda cls, e: fake_pod))

    def test_data_given_writes(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.write_mem.return_value = {"ok": True, "addr": 0x20000000, "length": 2}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_mem("lab", 0x20000000, data="beef")
        fake_pod.write_mem.assert_called_once()
        fake_pod.read_mem.assert_not_called()
        assert result["ok"] is True

    def test_no_data_no_out_path_reads_inline(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.read_mem.return_value = {"ok": True, "hex": "aabb"}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_mem("lab", 0x20000000, length=2)
        fake_pod.read_mem.assert_called_once_with(0x20000000, 2)
        fake_pod.write_mem.assert_not_called()
        assert result["hex"] == "aabb"

    def test_out_path_given_streams_to_file(self, monkeypatch, tmp_path):
        fake_pod = MagicMock()
        out = str(tmp_path / "out.bin")
        fake_pod.read_dut.return_value = out
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_mem("lab", 0x20000000, length=4096, out_path=out)
        fake_pod.read_dut.assert_called_once_with(0x20000000, 4096, out)
        fake_pod.read_mem.assert_not_called()
        assert result == out

    def test_read_without_length_raises(self, monkeypatch):
        fake_pod = MagicMock()
        self._patch(monkeypatch, fake_pod)
        with pytest.raises(ValueError):
            m.handle_dut_mem("lab", 0x20000000)


class TestMcpDutLinkRouting:
    def _patch(self, monkeypatch, fake_pod):
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry", classmethod(lambda cls, e: fake_pod))

    def test_action_up_attaches(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.usbip_attach.return_value = {
            "busid": "1-1", "vid": "f055", "pid": "9802", "tty": "/dev/ttyACM0"}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_link("lab", action="up")
        fake_pod.usbip_attach.assert_called_once_with(ensure=True)
        assert result["tty"] == "/dev/ttyACM0"

    def test_action_down_detaches(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.usbip_detach.return_value = {"detached": [0]}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_link("lab", action="down")
        fake_pod.usbip_detach.assert_called_once_with()
        assert result["detached"] == [0]

    def test_action_reprobe_reprobes(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.reprobe_dut.return_value = {"ok": True, "mounted": 1}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_link("lab", action="reprobe")
        fake_pod.reprobe_dut.assert_called_once_with(force=False)
        assert result["ok"] is True

    def test_action_status_reports_exports_and_attached_ports(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.usbip_list.return_value = [
            {"busid": "1-1", "vid": "f055", "pid": "9802"}]
        fake_pod.attached_ports.return_value = [0]
        self._patch(monkeypatch, fake_pod)
        result = m.handle_dut_link("lab", action="status")
        assert result["exported"] == fake_pod.usbip_list.return_value
        assert result["attached_ports"] == [0]

    def test_action_status_is_a_pure_read(self, monkeypatch):
        """status is the default action, so it must not be able to activate the
        pod's USB host: doing so has been observed to drop the pod's Wi-Fi, which
        is its only management channel. Spy on the activation path rather than
        stubbing it, so a regression fails here instead of passing silently."""
        calls = []
        monkeypatch.setattr("pod.usbip.ensure_server",
                            lambda pod, *a, **k: calls.append("ensure_server"))
        fake_pod = MagicMock()
        fake_pod.usbip_list.return_value = []
        fake_pod.attached_ports.return_value = []
        self._patch(monkeypatch, fake_pod)

        m.handle_dut_link("lab", action="status")

        assert calls == [], "status activated the pod USB host"
        fake_pod.usbip_attach.assert_not_called()
        fake_pod.reprobe_dut.assert_not_called()
        fake_pod.usbip_detach.assert_not_called()

    def test_unknown_action_raises(self):
        with pytest.raises(ValueError):
            m.handle_dut_link("lab", action="bogus")


class TestMcpBenchDeviceRouting:
    def _patch(self, monkeypatch, fake_pod):
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry", classmethod(lambda cls, e: fake_pod))

    def test_i2c_up(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.i2c_target.return_value = {"ok": True}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus="i2c", action="up")
        fake_pod.i2c_target.assert_called_once()
        assert result["ok"] is True

    def test_i2c_status_reads_peripheral_list(self, monkeypatch):
        # peripherals.instances() answers {"ok": ..., "instances": [name, ...]},
        # a list of names rather than a mapping keyed by name.
        fake_pod = MagicMock()
        fake_pod.peripheral_list.return_value = {
            "ok": True, "instances": ["i2c_target"]}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus="i2c", action="status")
        assert result["present"] is True
        assert result["name"] == "i2c_target"

    def test_i2c_status_reports_absent_instance(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.peripheral_list.return_value = {"ok": True, "instances": []}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus="i2c", action="status")
        assert result["present"] is False

    def test_spi_up(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.spi_target.return_value = {"ok": True, "size": 1024}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus="spi", action="up")
        fake_pod.spi_target.assert_called_once()
        assert result["ok"] is True

    def test_spi_status(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.spi_target_status.return_value = {"ok": True, "bytes_rx": 3}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus="spi", action="status")
        fake_pod.spi_target_status.assert_called_once()
        assert result["bytes_rx"] == 3

    def test_down_with_explicit_name_ignores_bus_and_ownership(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.peripheral_release.return_value = {"ok": True}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus=None, action="down",
                                       name="spi_target")
        fake_pod.peripheral_release.assert_called_once_with(name="spi_target")
        assert result["ok"] is True

    def test_down_wildcard_with_nothing_owned_releases_nothing(self, monkeypatch):
        # No prior action="up" in this process, so there is nothing tracked as
        # this caller's own; an unscoped sweep would deinit another agent's
        # live instance, so the default is a no-op that says so.
        fake_pod = MagicMock()
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus=None, action="down")
        fake_pod.peripheral_release.assert_not_called()
        assert result["released"] == []

    def test_down_wildcard_releases_only_what_this_caller_brought_up(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.i2c_target.return_value = {"ok": True}
        fake_pod.peripheral_release.return_value = {"ok": True, "released": ["i2c_target"]}
        self._patch(monkeypatch, fake_pod)
        m.handle_bench_device("lab", bus="i2c", action="up")
        result = m.handle_bench_device("lab", bus=None, action="down")
        fake_pod.peripheral_release.assert_called_once_with(name="i2c_target")
        assert result["released"] == ["i2c_target"]

    def test_down_wildcard_force_sweeps_everyone_and_reports_stole_from(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.i2c_target.return_value = {"ok": True}
        fake_pod.peripheral_list.return_value = {
            "ok": True, "instances": ["i2c_target", "spi_target"]}
        fake_pod.peripheral_release.return_value = {
            "ok": True, "released": ["i2c_target", "spi_target"]}
        self._patch(monkeypatch, fake_pod)
        m.handle_bench_device("lab", bus="i2c", action="up")  # ours
        result = m.handle_bench_device("lab", bus=None, action="down", force=True)
        fake_pod.peripheral_release.assert_called_once_with(name="*")
        assert result["stole_from"] == ["spi_target"]

    def test_up_without_bus_raises(self, monkeypatch):
        fake_pod = MagicMock()
        self._patch(monkeypatch, fake_pod)
        with pytest.raises(ValueError):
            m.handle_bench_device("lab", bus=None, action="up")

    def test_status_without_bus_raises(self, monkeypatch):
        fake_pod = MagicMock()
        self._patch(monkeypatch, fake_pod)
        with pytest.raises(ValueError):
            m.handle_bench_device("lab", bus=None, action="status")


# ── CLI tree: every 'pod dut <verb>' / 'pod bench <verb>' parses ───────────
#
# Every verb's subparser requires `label` first; _require_pod(label) is the
# first thing every cmd_dut_*/cmd_bench_* function does, so an unregistered
# label reaches application code and exits 1. A structurally broken argv
# (wrong/missing subcommand, missing required flag) exits 2 from argparse
# itself before application code runs at all - the two are distinguished by
# exit code, not by SystemExit alone.

DUT_VERB_ARGS = {
    "open": ["nope", "/dev/ttyACM0"],
    "exec": ["nope", "print(1)"],
    "flash": ["nope", "fw.bin"],
    "erase": ["nope"],
    "reset": ["nope"],
    "reg": ["nope", "r0"],
    "mem": ["nope", "0x20000000"],
    "halt": ["nope"],
    "resume": ["nope"],
    "gdb": ["nope"],
    "identify": ["nope"],
    "link": ["nope"],
}

BENCH_VERB_ARGS = {
    "gpio": ["nope", "5"],
    "adc": ["nope", "5"],
    "la": ["nope"],
    "device": ["nope"],
    "device-regs": ["nope", "--bus", "spi"],
    "uart": ["nope"],
}


class TestCliTreeParsesEveryVerb:
    @pytest.fixture(autouse=True)
    def isolated_registry(self, tmp_path, monkeypatch):
        """Route registry I/O to a tmp directory so 'nope' is never registered."""
        monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
        yield tmp_path

    @pytest.mark.parametrize("verb", sorted(DUT_VERB_ARGS))
    def test_dut_verb_reaches_application_code(self, monkeypatch, verb):
        monkeypatch.setattr(sys, "argv", ["pod", "dut", verb, *DUT_VERB_ARGS[verb]])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    @pytest.mark.parametrize("verb", sorted(BENCH_VERB_ARGS))
    def test_bench_verb_reaches_application_code(self, monkeypatch, verb):
        monkeypatch.setattr(sys, "argv", ["pod", "bench", verb, *BENCH_VERB_ARGS[verb]])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


# ── the CLI's live subcommand set is exactly the contract ──────────────────


class TestCliSubcommandSetIsExact:
    """argparse's own 'invalid choice' error lists the live choice set for a
    subparser; asserting it exactly, rather than sampling individual names,
    catches any stray leftover as well as any accidental extra."""

    def _choices_from_error(self, capsys, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        match = re.search(r"choose from ([^)]+)\)", err)
        assert match, err
        return {tok.strip().strip("'") for tok in match.group(1).split(",")}

    def test_top_level_choices_are_pod_verbs_and_the_two_groups(self, monkeypatch, capsys):
        choices = self._choices_from_error(
            capsys, monkeypatch, ["pod", "__nope__"])
        assert choices == {
            "discover", "list", "register", "unregister", "info", "exec",
            "mount", "cp", "pins", "flm", "install-udev", "open", "open-raw",
            "dut", "bench",
        }
        # Verbs that address the DUT or the bench instruments are reachable only
        # under their group, never at the top level. `open` is absent from this
        # set deliberately: `pod open` is the pod's own session, the CLI spelling
        # of pod_open, and `pod dut open` is the DUT's.
        grouped_only = {"flash", "reset", "reprobe", "gdb", "spi-target",
                        "spi-target-status", "spi-target-regs", "i2c-target",
                        "i2c-target-regs", "gpio", "adc", "la", "uart", "link",
                        "halt", "resume", "reg", "mem", "identify",
                        "attach", "detach"}
        assert choices & grouped_only == set()

    def test_dut_subcommand_choices_are_exactly_the_12_verbs(self, monkeypatch, capsys):
        choices = self._choices_from_error(
            capsys, monkeypatch, ["pod", "dut", "__nope__", "x"])
        assert choices == set(DUT_VERB_ARGS)

    def test_bench_subcommand_choices_are_exactly_the_6_verbs(self, monkeypatch, capsys):
        choices = self._choices_from_error(
            capsys, monkeypatch, ["pod", "bench", "__nope__", "x"])
        assert choices == set(BENCH_VERB_ARGS)


# ── mutually exclusive arguments refuse rather than silently pick one ───────


class TestMutuallyExclusiveArgsRefuse:
    """A merged tool takes arguments that used to belong to separate tools, so
    a caller can now name two incompatible intentions in one call. Each such
    pair must refuse: silently honouring one and dropping the other turns a
    read request into a write, or discards the payload."""

    def _patch(self, monkeypatch, fake_pod):
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: fake_pod))

    def test_dut_mem_refuses_data_with_out_path(self, monkeypatch):
        fake_pod = MagicMock()
        self._patch(monkeypatch, fake_pod)
        with pytest.raises(ValueError, match="mutually exclusive"):
            m.handle_dut_mem("lab", addr=0x20000000, data="ff00",
                             out_path="/tmp/dump.bin")
        fake_pod.write_mem.assert_not_called()

    def test_session_send_refuses_data_with_control(self):
        with pytest.raises(ValueError, match="not both"):
            m.handle_session_send("lab", data="print(1)", control="c")

    def test_session_send_rejects_unknown_control(self):
        with pytest.raises(ValueError, match="control must be"):
            m.handle_session_send("lab", control="z")


# ── a session is never returned for the wrong subject ──────────────────────


class TestSessionsAreKeyedBySubject:
    """A session id is derived from what it is attached to, so a pod session and
    any number of DUT sessions coexist, and re-opening a target hands back the
    session already held instead of building a second one against it."""

    def _held(self, monkeypatch, device):
        session = MagicMock()
        session.running = True
        session.target = device or "socket://pod:8266"
        session.mounted = False
        monkeypatch.setitem(
            m._SESSIONS, m._session_id("lab", device),
            {"session": session, "log_path": "/tmp/x.log", "device": device,
             "label": "lab"})
        return session

    def test_ids_separate_the_pod_from_each_dut(self):
        assert m._session_id("lab") == "lab:pod"
        assert m._session_id("lab", "/dev/ttyACM0") == "lab:dut:ttyACM0"
        assert m._session_id("lab", "/dev/ttyACM1") == "lab:dut:ttyACM1"
        assert m._session_id("lab") != m._session_id("lab", "/dev/ttyACM0")

    def test_a_pod_session_does_not_block_a_dut_session(self, monkeypatch):
        self._held(monkeypatch, None)
        opened = {}
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock(
            open_session=lambda **kw: (opened.update(kw), _fake_session())[1]))
        info = m.handle_dut_open("lab", device="/dev/ttyACM0")
        assert info["session"] == "lab:dut:ttyACM0"
        # the pod session is untouched and still held
        assert "lab:pod" in m._SESSIONS

    def test_a_dut_session_does_not_block_the_pod_session(self, monkeypatch):
        self._held(monkeypatch, "/dev/ttyACM0")
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock(
            open_session=lambda **kw: _fake_session()))
        info = m.handle_pod_open("lab")
        assert info["session"] == "lab:pod"
        assert "lab:dut:ttyACM0" in m._SESSIONS

    def test_two_duts_on_one_pod_coexist(self, monkeypatch):
        self._held(monkeypatch, "/dev/ttyACM0")
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock(
            open_session=lambda **kw: _fake_session()))
        info = m.handle_dut_open("lab", device="/dev/ttyACM1")
        assert info["session"] == "lab:dut:ttyACM1"
        assert "lab:dut:ttyACM0" in m._SESSIONS

    def test_reopening_the_same_target_returns_the_held_session(self, monkeypatch):
        self._held(monkeypatch, "/dev/ttyACM0")
        info = m.handle_dut_open("lab", device="/dev/ttyACM0")
        assert info["already_open"] is True
        assert info["session"] == "lab:dut:ttyACM0"

    def test_session_verbs_refuse_an_unknown_id(self):
        with pytest.raises(KeyError, match="No open session"):
            m.handle_session_read("lab:dut:nope")


def _fake_session():
    s = MagicMock()
    s.running = True
    s.target = "/dev/ttyACM9"
    s.mounted = False
    return s


# ── the un-stick path stayed reachable through dut_open ────────────────────


class TestDutOpenRecover:
    def test_recover_runs_before_connecting_and_is_reported(self, monkeypatch):
        order = []
        verdict = {"ok": True, "device": "/dev/ttyACM0", "recovered": True,
                   "prompt_seen": True, "was_raw": True, "output": ">>> "}
        fake_pod = MagicMock()
        fake_pod.recover_dut_repl.side_effect = \
            lambda dev, *a, **k: (order.append("recover"), verdict)[1]
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: fake_pod))
        monkeypatch.setattr(
            m, "_open_session",
            lambda *a, **k: (order.append("open"), {"running": True})[1])

        result = m.handle_dut_open("lab", device="/dev/ttyACM0", recover=True)

        assert order == ["recover", "open"]
        assert result["recover"] == verdict

    def test_recover_defaults_off(self, monkeypatch):
        fake_pod = MagicMock()
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: fake_pod))
        monkeypatch.setattr(m, "_open_session",
                            lambda *a, **k: {"running": True})

        result = m.handle_dut_open("lab", device="/dev/ttyACM0")

        fake_pod.recover_dut_repl.assert_not_called()
        assert "recover" not in result


# ── handler layer mirrors the tool surface exactly ─────────────────────────


class TestHandlerNamesMirrorTools:
    """Every public handle_* function is one of the 28 tools and every tool has
    one, so the importable Python API and the MCP surface name the same set of
    operations. Helpers that are not tools are private."""

    def _public_handlers(self):
        return {n[len("handle_"):] for n in dir(m)
                if n.startswith("handle_") and callable(getattr(m, n))}

    def test_every_tool_has_a_handler(self):
        assert EXPECTED_TOOLS - self._public_handlers() == set()

    def test_every_public_handler_is_a_tool(self):
        assert self._public_handlers() - EXPECTED_TOOLS == set()

    def test_no_public_handler_carries_a_name_off_the_surface(self):
        assert self._public_handlers() & REMOVED_TOOLS == set()


# ── schema declarations an agent relies on ─────────────────────────────────


class TestSchemaDeclarations:
    def _schema(self, tool):
        src = _build_server_source()
        found = re.search(
            r'name="%s".*?inputSchema=\{(.*?)\n                \},\n            \)'
            % tool, src, re.S)
        assert found, "no inputSchema found for %s" % tool
        return found.group(1)

    @pytest.mark.parametrize("tool", ["dut_link", "bench_device"])
    def test_action_is_not_required_so_its_default_is_reachable(self, tool):
        schema = self._schema(tool)
        required = re.search(r'"required": \[([^\]]*)\]', schema).group(1)
        assert '"action"' not in required
        assert '"default"' in schema

    def test_bench_device_keeps_its_numeric_bounds(self):
        schema = self._schema("bench_device")
        assert '"minimum": 1, "maximum": 8192' in schema      # size
        assert '"minimum": 1, "maximum": 4096' in schema      # table_size

    @pytest.mark.parametrize("tool", _tool_list_names())
    def test_every_schema_rejects_unknown_properties(self, tool):
        # A misspelled optional argument (e.g. i2cbus for i2c_bus) is
        # otherwise silently dropped rather than reported, so an agent would
        # get the default value with no indication its argument was ignored.
        schema = self._schema(tool)
        assert '"additionalProperties": False' in schema

    def test_unknown_property_is_rejected_before_the_handler_runs(self):
        import asyncio
        import mcp.types as t
        srv = m.build_server()
        handler = srv.request_handlers[t.CallToolRequest]
        req = t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(
                name="bench_device",
                arguments={"label": "lab", "bus": "i2c", "action": "up",
                           "i2cbus": 7}))
        result = asyncio.run(handler(req)).root
        assert result.isError
        assert "i2cbus" in result.content[0].text


# ── dut_exec takes the cheapest route to the DUT ───────────────────────────


class TestDutExecRoute:
    """dut_exec reports which route it took as `via`. The session route exists
    to remove the attach/detach cycle from the inner loop, so the test asserts
    the cycle is not run, not merely that the call succeeded."""

    def _session(self, monkeypatch, device="/dev/ttyACM0", running=True):
        sess = MagicMock()
        sess.running = running
        sess.target = device
        sess.tell.return_value = 0
        sess.send.return_value = 12
        sess.read_since.return_value = {"text": "42\r\n", "cursor": 6,
                                        "dropped": 0}
        monkeypatch.setitem(
            m._SESSIONS, m._session_id("lab", device),
            {"session": sess, "log_path": "/tmp/x.log", "device": device,
             "label": "lab"})
        return sess

    def test_open_session_is_reused_and_nothing_is_attached(self, monkeypatch):
        self._session(monkeypatch)
        fake_pod = MagicMock()
        monkeypatch.setattr(m, "_pod_for", lambda label: fake_pod)

        result = m.handle_dut_exec("lab", "print(6*7)", wait=0)

        assert result["via"] == "session"
        assert result["session"] == "lab:dut:ttyACM0"
        assert result["device"] == "/dev/ttyACM0"
        # the whole point: no link work at all
        fake_pod.dut_exec.assert_not_called()
        fake_pod.usbip_attach.assert_not_called()
        fake_pod.usbip_detach.assert_not_called()

    def test_code_goes_down_the_session_as_one_exec_line(self, monkeypatch):
        sess = self._session(monkeypatch)
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock())

        m.handle_dut_exec("lab", "for i in range(2):\n    print(i)", wait=0)

        sent = sess.send.call_args[0][0]
        assert sent.startswith("exec(")
        # one line, so line-by-line submission cannot break the indentation
        assert "\n" not in sent
        assert "for i in range(2):" in sent

    def test_falls_back_to_attach_with_no_session(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.dut_exec.return_value = {
            "tty": "/dev/ttyACM0", "returncode": 0, "stdout": "42\n",
            "stderr": "", "reattached": True}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake_pod)

        result = m.handle_dut_exec("lab", "print(6*7)")

        assert result["via"] == "attach"
        assert result["stdout"] == "42\n"
        fake_pod.dut_exec.assert_called_once_with("print(6*7)")

    def test_a_closed_session_is_not_reused(self, monkeypatch):
        self._session(monkeypatch, running=False)
        fake_pod = MagicMock()
        fake_pod.dut_exec.return_value = {"tty": "/dev/ttyACM0",
                                          "returncode": 0, "stdout": "",
                                          "stderr": "", "reattached": True}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake_pod)

        assert m.handle_dut_exec("lab", "x")["via"] == "attach"

    def test_two_dut_sessions_refuse_to_be_guessed_between(self, monkeypatch):
        self._session(monkeypatch, device="/dev/ttyACM0")
        self._session(monkeypatch, device="/dev/ttyACM1")
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock())

        with pytest.raises(ValueError, match="holds 2 DUT sessions"):
            m.handle_dut_exec("lab", "print(1)")

    def test_a_pod_session_is_not_mistaken_for_a_dut_session(self, monkeypatch):
        # a pod session must not be used to run DUT code
        sess = MagicMock()
        sess.running = True
        monkeypatch.setitem(
            m._SESSIONS, m._session_id("lab"),
            {"session": sess, "log_path": "/tmp/x.log", "device": None,
             "label": "lab"})
        fake_pod = MagicMock()
        fake_pod.dut_exec.return_value = {"tty": "/dev/ttyACM0",
                                          "returncode": 0, "stdout": "",
                                          "stderr": "", "reattached": True}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake_pod)

        assert m.handle_dut_exec("lab", "x")["via"] == "attach"
        sess.send.assert_not_called()


# ── dut_open reaches a DUT in one call ─────────────────────────────────────


class TestDutOpenBringsTheLinkUp:
    def _link_down(self, monkeypatch):
        """No attachment held, so a bring-up is genuinely required."""
        monkeypatch.setattr(m, "_pod_for",
                            lambda label: MagicMock(attached_ports=lambda: []))
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)

    def test_device_omitted_brings_the_link_up_and_uses_its_tty(self, monkeypatch):
        opened = {}
        fake = MagicMock()
        fake.running, fake.target, fake.mounted = True, "/dev/ttyACM3", False
        pod = MagicMock()
        pod.attached_ports.return_value = []
        pod.dut_tty.return_value = "/dev/ttyACM3"
        pod.open_session = lambda **kw: (opened.update(kw), fake)[1]
        monkeypatch.setattr(m, "_pod_for", lambda label: pod)
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)

        info = m.handle_dut_open("lab")

        assert opened["device"] == "/dev/ttyACM3"
        assert info["session"] == "lab:dut:ttyACM3"
        assert info["link"]["tty"] == "/dev/ttyACM3"

    def test_device_given_does_not_touch_the_link(self, monkeypatch):
        calls = []
        monkeypatch.setattr(m, "handle_dut_link",
                            lambda *a, **k: calls.append(a) or {})
        fake = MagicMock()
        fake.running = True
        fake.target = "/dev/ttyACM0"
        fake.mounted = False
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock(
            open_session=lambda **kw: fake))

        info = m.handle_dut_open("lab", device="/dev/ttyACM0")

        assert calls == []
        assert "link" not in info

    def test_a_bring_up_with_no_tty_is_an_error_not_a_broken_session(self, monkeypatch):
        pod = MagicMock()
        pod.attached_ports.return_value = []
        pod.dut_tty.side_effect = RuntimeError("no CDC tty appeared")
        monkeypatch.setattr(m, "_pod_for", lambda label: pod)
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)
        with pytest.raises(RuntimeError, match="no CDC tty"):
            m.handle_dut_open("lab")

    def test_a_bring_up_is_refused_while_our_own_pod_session_holds_the_repl(
            self, monkeypatch):
        """Bringing the link up runs code on the pod, so it needs the pod's one
        REPL slot. Holding that slot ourselves has to be reported as the conflict
        it is, not left to surface as a transport error from the pod."""
        self._link_down(monkeypatch)
        held = MagicMock()
        held.running = True
        monkeypatch.setitem(m._SESSIONS, "lab:pod",
                            {"session": held, "log_path": "/tmp/x.log",
                             "device": None, "label": "lab"})
        monkeypatch.setattr(m, "handle_dut_link",
                            lambda *a, **k: pytest.fail("attempted a bring-up"))
        with pytest.raises(ValueError, match="holding it"):
            m.handle_dut_open("lab")

    def test_an_already_attached_link_needs_no_pod_repl(self, monkeypatch):
        """The converse: with the link already up there is nothing to run on the
        pod, so a held pod session must not block opening the DUT session."""
        pod = MagicMock()
        pod.attached_ports.return_value = [0]
        pod.dut_tty.return_value = "/dev/ttyACM0"
        monkeypatch.setattr(m, "_pod_for", lambda label: pod)
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: "/dev/ttyACM0")
        held = MagicMock()
        held.running = True
        monkeypatch.setitem(m._SESSIONS, "lab:pod",
                            {"session": held, "log_path": "/tmp/x.log",
                             "device": None, "label": "lab"})
        monkeypatch.setattr(m, "_open_session",
                            lambda *a, **k: {"running": True})

        info = m.handle_dut_open("lab")
        assert info["link"]["tty"] == "/dev/ttyACM0"


# ── the tty a caller already holds is not re-attached ──────────────────────


class TestDutTtyIsTheOneAcquisitionPath:
    """The CLI and the MCP handler both ask for the DUT's tty, so they share one
    implementation: two copies drifted into two behaviours and two messages."""

    def test_a_held_attachment_is_used_without_attaching(self, monkeypatch):
        from pod.client import Pod
        pod = Pod(address="10.0.0.1", repl_port=8266)
        monkeypatch.setattr(pod, "attached_ports", lambda: [0])
        monkeypatch.setattr("pod.usbip.forwarded_tty",
                            lambda *a, **k: "/dev/ttyACM0")
        monkeypatch.setattr(pod, "usbip_attach",
                            lambda *a, **k: pytest.fail("attached needlessly"))
        assert pod.dut_tty() == "/dev/ttyACM0"

    def test_nothing_held_means_attach(self, monkeypatch):
        from pod.client import Pod
        pod = Pod(address="10.0.0.1", repl_port=8266)
        monkeypatch.setattr(pod, "attached_ports", lambda: [])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)
        monkeypatch.setattr(pod, "usbip_attach",
                            lambda ensure=True: {"busid": "1-1",
                                                 "tty": "/dev/ttyACM3"})
        assert pod.dut_tty() == "/dev/ttyACM3"

    def test_an_attach_with_no_tty_is_a_named_error(self, monkeypatch):
        from pod.client import Pod
        pod = Pod(address="10.0.0.1", repl_port=8266)
        monkeypatch.setattr(pod, "attached_ports", lambda: [])
        monkeypatch.setattr("pod.usbip.forwarded_tty", lambda *a, **k: None)
        monkeypatch.setattr(pod, "usbip_attach",
                            lambda ensure=True: {"busid": "1-1", "tty": None})
        with pytest.raises(RuntimeError, match="no CDC tty"):
            pod.dut_tty()


class TestSessionIdStaysPassable:
    """A session id has to name one device unambiguously and stay short enough to
    hand around. Ids key on the CANONICAL device, so the same tty spelled two ways
    is one session; a device path that does not resolve is used as written, and a
    long one is truncated with a digest so two devices cannot share an id."""

    # Deliberately a path that exists on no host, so realpath leaves it alone and
    # the test measures the truncation rather than the machine it runs on.
    SYNTH = ("/dev/serial/by-id/usb-SynthVendor_SynthBoard_"
             "0501083219160908-if01")

    def test_a_short_tty_is_left_readable(self):
        assert m._session_id("lab", "/dev/ttyACM0") == "lab:dut:ttyACM0"

    def test_an_unresolvable_long_path_is_bounded(self):
        sid = m._session_id("lab", self.SYNTH)
        assert len(sid) < 45, sid
        # the serial and the interface are what distinguish devices; keep them
        assert "0501083219160908" in sid
        assert "if01" in sid

    def test_ids_are_stable_across_calls(self):
        assert m._session_id("lab", self.SYNTH) == m._session_id("lab", self.SYNTH)

    def test_devices_sharing_a_tail_do_not_collide(self):
        a = "/dev/serial/by-id/usb-VendorAlpha_Widget_0000000000000001-if00"
        b = "/dev/serial/by-id/usb-VendorBeta_Gadget_0000000000000001-if00"
        assert a[-24:] == b[-24:]              # same tail, different device
        assert m._session_id("lab", a) != m._session_id("lab", b)

    def test_one_device_spelled_two_ways_is_one_session(self, tmp_path):
        """The attach path resolves a by-id link to its real node while looking up
        a held attachment returns the link, so keying on the raw string would give
        one DUT two ids and open a second reader on the same tty."""
        node = tmp_path / "ttyACM9"
        node.write_text("")
        link = tmp_path / "by-id-alias"
        link.symlink_to(node)
        assert m._session_id("lab", str(link)) == m._session_id("lab", str(node))

    def test_an_empty_device_is_not_a_dut(self):
        """Empty must read as absent: the session layer falls back to the pod's
        socket REPL when no device is given, so passing it through would open a
        pod session wearing a DUT id."""
        for blank in ("", "   ", None):
            assert m._session_id("lab", blank) == "lab:pod"

    def test_a_trailing_slash_is_the_same_device(self):
        assert m._session_id("lab", "/dev/ttyACM0/") == \
            m._session_id("lab", "/dev/ttyACM0")

    def test_the_log_path_inherits_the_bound(self):
        assert len(m._default_repl_log("lab", self.SYNTH)) < 80
        assert m._default_repl_log("lab", None) != \
            m._default_repl_log("lab", self.SYNTH)




# ── the two dut_exec routes report the same way ────────────────────────────


class TestDutExecRouteShape:
    """The session route used to return the DUT's output as `text` with no status,
    so a traceback on the DUT was indistinguishable from success."""

    def _session(self, monkeypatch, text):
        sess = MagicMock()
        sess.running = True
        sess.connected = True
        sess.target = "/dev/ttyACM0"
        sess.tell.return_value = 0
        sess.send.return_value = 1
        sess.read_since.return_value = {"text": text, "cursor": len(text),
                                        "dropped": 0}
        monkeypatch.setitem(
            m._SESSIONS, m._session_id("lab", "/dev/ttyACM0"),
            {"session": sess, "log_path": "/tmp/x.log",
             "device": "/dev/ttyACM0", "label": "lab"})
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock())
        return sess

    def _echo(self, code):
        return m._session_exec_line(code)

    def test_success_reports_zero_and_clean_stdout(self, monkeypatch):
        code = "print(6*7)"
        self._session(monkeypatch, "%s\r\n42\r\n%s\r\n>>> " % (self._echo(code), m._OK))
        r = m.handle_dut_exec("lab", code, wait=0)
        assert r["via"] == "session"
        assert r["returncode"] == 0
        assert r["stdout"] == "42"          # echo, sentinel and prompt stripped
        assert r["stderr"] == ""

    def test_a_raising_body_reports_nonzero_with_the_exception(self, monkeypatch):
        code = "1/0"
        self._session(monkeypatch, "%s\r\n%s ZeroDivisionError('divide by zero',)\r\n>>> "
                      % (self._echo(code), m._ERR))
        r = m.handle_dut_exec("lab", code, wait=0)
        assert r["returncode"] == 1
        assert "ZeroDivisionError" in r["stderr"]
        assert r["stdout"] == ""

    def test_output_without_a_trailing_newline_still_parses(self, monkeypatch):
        """The sentinel is only matched at line start, so it has to be printed
        onto a line of its own even when the body left the cursor mid-line."""
        code = "print('x', end='')"
        self._session(monkeypatch,
                      "%s\r\nx\r\n%s\r\n>>> " % (self._echo(code), m._OK))
        r = m.handle_dut_exec("lab", code, wait=0)
        assert r["returncode"] == 0
        assert r["stdout"] == "x"

    def test_silence_is_reported_as_unknown_not_success(self, monkeypatch):
        """No sentinel means the DUT said nothing in time. That is not success:
        reporting 0 here would make a hung DUT look like a clean run."""
        self._session(monkeypatch, "")
        r = m.handle_dut_exec("lab", "print(1)", wait=0)
        assert r["returncode"] is None

    def test_the_echoed_command_does_not_trip_the_sentinels(self, monkeypatch):
        """The friendly REPL echoes what it was sent, and the echo contains both
        sentinels as part of the source, so they only count at line start."""
        code = "print(1)"
        self._session(monkeypatch, "%s\r\n1\r\n%s\r\n>>> " % (self._echo(code), m._OK))
        r = m.handle_dut_exec("lab", code, wait=0)
        assert r["returncode"] == 0
        assert m._OK not in r["stdout"] and m._ERR not in r["stdout"]
        assert "exec(" not in r["stdout"]

    def test_both_routes_share_their_result_keys(self, monkeypatch):
        code = "print(1)"
        self._session(monkeypatch, "%s\r\n1\r\n%s\r\n>>> " % (self._echo(code), m._OK))
        sess_keys = set(m.handle_dut_exec("lab", code, wait=0))
        m._SESSIONS.clear()
        fake = MagicMock()
        fake.dut_exec.return_value = {"tty": "/dev/ttyACM0", "returncode": 0,
                                      "stdout": "1\n", "stderr": "",
                                      "reattached": False}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)
        att_keys = set(m.handle_dut_exec("lab", code))
        for key in ("via", "returncode", "stdout", "stderr"):
            assert key in sess_keys and key in att_keys, key


class TestDutSessionLiveness:
    def _held(self, monkeypatch, connected):
        sess = MagicMock()
        sess.running = True
        sess.connected = connected
        monkeypatch.setitem(
            m._SESSIONS, m._session_id("lab", "/dev/ttyACM0"),
            {"session": sess, "log_path": "/tmp/x.log",
             "device": "/dev/ttyACM0", "label": "lab"})
        return sess

    def test_a_reconnecting_session_is_neither_used_nor_bypassed(self, monkeypatch):
        """running only says the reader thread is alive, and with reconnect=True
        it stays alive retrying a tty that has gone away, so the session must not
        be written to. Nor is the attach route a fallback: the session still owns
        the tty, and the attach route's rebuild would detach the link it is
        waiting on."""
        sess = self._held(monkeypatch, connected=False)
        fake = MagicMock()
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)

        with pytest.raises(ValueError, match="link is down"):
            m.handle_dut_exec("lab", "print(1)")

        sess.send.assert_not_called()
        fake.dut_exec.assert_not_called()

    def test_no_session_at_all_still_takes_the_attach_route(self, monkeypatch):
        fake = MagicMock()
        fake.dut_exec.return_value = {"tty": "/dev/ttyACM0", "returncode": 0,
                                      "stdout": "", "stderr": "",
                                      "reattached": True}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)
        assert m.handle_dut_exec("lab", "print(1)")["via"] == "attach"


class TestDeadSessionIsClosedBeforeItsIdIsReused:
    def test_replacing_a_stopped_session_closes_it(self, monkeypatch):
        dead = MagicMock()
        dead.running = False
        monkeypatch.setitem(
            m._SESSIONS, "lab:pod",
            {"session": dead, "log_path": "/tmp/x.log", "device": None,
             "label": "lab"})
        fresh = MagicMock()
        fresh.running, fresh.target, fresh.mounted = True, "socket://x", False
        monkeypatch.setattr(m, "_pod_for", lambda label: MagicMock(
            open_session=lambda **kw: fresh))

        m.handle_pod_open("lab")

        dead.close.assert_called_once()     # log handle released, not orphaned
        assert m._SESSIONS["lab:pod"]["session"] is fresh


# ── a failure is returned as a failure, not as prose ───────────────────────


class TestToolResultsAreMachineReadable:
    """call_tool used to return every outcome as plain text through one path, so
    a refusal was indistinguishable from a success whose text began with
    "Error:". An agent branching on contention needs isError and a kind."""

    def _call(self, name, arguments):
        import asyncio
        import mcp.types as t
        srv = m.build_server()
        handler = srv.request_handlers[t.CallToolRequest]
        req = t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(name=name, arguments=arguments))
        return asyncio.run(handler(req)).root

    def _body(self, res):
        import json
        return json.loads(res.content[0].text)

    def test_a_success_is_json_and_not_an_error(self, monkeypatch):
        monkeypatch.setattr(m, "handle_pod_info",
                            lambda label: {"label": label, "sessions": []})
        res = self._call("pod_info", {"label": "lab"})
        assert not getattr(res, "isError", False)
        assert self._body(res)["label"] == "lab"

    def test_an_unknown_label_is_marked_as_an_error(self):
        res = self._call("pod_info", {"label": "__no_such_pod__"})
        assert res.isError is True
        assert self._body(res)["kind"] == "not_found"

    def test_a_bad_argument_is_marked_and_named(self, monkeypatch):
        """Schema-valid but refused at runtime. An out-of-enum value never
        reaches the handler: the SDK validates against inputSchema first and
        returns its own error, which is why this uses a pair the schema cannot
        express as mutually exclusive."""
        monkeypatch.setattr(m, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        monkeypatch.setattr(m.Pod, "from_entry",
                            classmethod(lambda cls, e: MagicMock()))
        res = self._call("dut_mem", {"label": "lab", "addr": 0x20000000,
                                     "data": "ff00", "out_path": "/tmp/x.bin"})
        assert res.isError is True
        body = self._body(res)
        assert body["kind"] == "invalid_argument"
        assert body["tool"] == "dut_mem"
        assert "mutually exclusive" in body["error"]

    def test_schema_validation_still_rejects_an_out_of_enum_value(self):
        """The SDK rejects it before the handler, so it is an error either way."""
        res = self._call("dut_link", {"label": "lab", "action": "bogus"})
        assert res.isError is True

    def test_contention_is_kind_busy_and_retryable(self, monkeypatch):
        from pod.client import PodExecError

        def busy(*a, **k):
            raise PodExecError("exec", 1, "",
                               "annealage-pod: BUSY - REPL in use by another client",
                               caller="agent-a:corona@carbon")

        monkeypatch.setattr(m, "handle_pod_exec", busy)
        res = self._call("pod_exec", {"label": "lab", "code": "print(1)"})
        assert res.isError is True
        body = self._body(res)
        assert body["kind"] == "busy"
        assert body["retryable"] is True
        assert body["caller"] == "agent-a:corona@carbon"

    def test_a_real_pod_fault_is_not_marked_retryable(self, monkeypatch):
        from pod.client import PodExecError

        def broken(*a, **k):
            raise PodExecError("exec", 1, "", "could not enter raw repl")

        monkeypatch.setattr(m, "handle_pod_exec", broken)
        body = self._body(self._call("pod_exec", {"label": "lab", "code": "x"}))
        assert body["kind"] == "pod_exec_failed"
        assert body["retryable"] is False

    def test_an_unexpected_exception_is_still_an_error_not_a_result(self, monkeypatch):
        """Anything uncaught used to escape as a success-shaped response."""
        def boom(*a, **k):
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(m, "handle_pod_info", boom)
        res = self._call("pod_info", {"label": "lab"})
        assert res.isError is True
        assert "unforeseen" in self._body(res)["error"]

    def test_a_pre_emptive_refusal_is_kind_conflict_and_names_the_holder(self, monkeypatch):
        """The phase-6 anti-bump gate refuses host-side, before any pod round
        trip, so it is a distinct kind from "busy" (a pod-reported REPL
        collision) even though both mean "wait or ask, do not reset"."""
        from pod.client import PodConflictError

        def refused(*a, **k):
            raise PodConflictError("usbip", {"caller": "agent-b:corona@carbon"},
                                   caller="agent-a:corona@carbon")

        monkeypatch.setattr(m, "handle_dut_flash", refused)
        res = self._call("dut_flash", {"label": "lab", "image": "fw.bin"})
        assert res.isError is True
        body = self._body(res)
        assert body["kind"] == "conflict"
        assert body["resource"] == "usbip"
        assert body["retryable"] is True
        assert body["held_by"] == {"caller": "agent-b:corona@carbon"}
        assert body["caller"] == "agent-a:corona@carbon"


class TestForceArgReachesTheHandler:
    """call_tool threads its handler args positionally, so a new parameter
    inserted in the wrong spot silently shifts every argument after it rather
    than raising - assert force actually arrives, not just that the call
    does not crash."""

    def _call(self, name, arguments):
        import asyncio
        import mcp.types as t
        srv = m.build_server()
        handler = srv.request_handlers[t.CallToolRequest]
        req = t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(name=name, arguments=arguments))
        return asyncio.run(handler(req)).root

    def test_dut_flash_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_flash", fake)
        self._call("dut_flash", {"label": "lab", "image": "fw.bin", "force": True})
        assert fake.call_args[0][-1] is True

    def test_dut_flash_force_defaults_false(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_flash", fake)
        self._call("dut_flash", {"label": "lab", "image": "fw.bin"})
        assert fake.call_args[0][-1] is False

    def test_dut_erase_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_erase", fake)
        self._call("dut_erase", {"label": "lab", "force": True})
        assert fake.call_args[0][-1] is True

    def test_dut_reset_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_reset", fake)
        self._call("dut_reset", {"label": "lab", "force": True})
        assert fake.call_args[0][-1] is True

    def test_dut_link_reprobe_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_link", fake)
        self._call("dut_link", {"label": "lab", "action": "reprobe", "force": True})
        assert fake.call_args[0][-1] is True

    def test_dut_flm_defaults_to_report_only(self, monkeypatch):
        fake = MagicMock(return_value={"installed": False})
        monkeypatch.setattr(m, "handle_dut_flm", fake)
        self._call("dut_flm", {"label": "lab"})
        assert fake.call_args[0] == ("lab", None, None, False, None, None, False)

    def test_dut_flm_threads_download_vendor_and_pack_name(self, monkeypatch):
        fake = MagicMock(return_value={"installed": True})
        monkeypatch.setattr(m, "handle_dut_flm", fake)
        self._call("dut_flm", {
            "label": "lab", "download": True, "vendor": "NordicSemiconductor",
            "pack_name": "nRF_DeviceFamilyPack"})
        assert fake.call_args[0] == (
            "lab", None, None, True, "NordicSemiconductor",
            "nRF_DeviceFamilyPack", False)

    def test_dut_flm_device_and_pack_land_in_the_right_slots(self, monkeypatch):
        # device and pack are adjacent string arguments in both the dispatch
        # call and handle_dut_flm's signature - distinct values in both here
        # so a positional swap between them would fail this, not just leave
        # None in place of None.
        fake = MagicMock(return_value={"installed": True})
        monkeypatch.setattr(m, "handle_dut_flm", fake)
        self._call("dut_flm", {
            "label": "lab", "device": "STM32F407VG", "pack": "/path/to/x.pack"})
        assert fake.call_args[0] == (
            "lab", "STM32F407VG", "/path/to/x.pack", False, None, None, False)

    def test_dut_flm_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"installed": True})
        monkeypatch.setattr(m, "handle_dut_flm", fake)
        self._call("dut_flm", {"label": "lab", "force": True})
        assert fake.call_args[0][-1] is True

    def test_bench_device_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_bench_device", fake)
        self._call("bench_device", {"label": "lab", "action": "down", "force": True})
        assert fake.call_args[0][-1] is True

    def test_dut_halt_force_true(self, monkeypatch):
        fake = MagicMock(return_value={"ok": True})
        monkeypatch.setattr(m, "handle_dut_halt", fake)
        self._call("dut_halt", {"label": "lab", "force": True})
        assert fake.call_args[0][-1] is True


class TestHandleDutFlm:
    """handle_dut_flm mirrors the CLI's cmd_flm exactly: report-only with no
    options, resolve+install given any of device/pack/download/force."""

    def _pod(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.flm_algo_info.return_value = {"installed": False}
        fake_pod.resolve_flm_algo.return_value = {"name": "resolved"}
        fake_pod.install_flm_algo.return_value = {"installed": True}
        monkeypatch.setattr(m, "_pod_for", lambda label: fake_pod)
        return fake_pod

    def test_no_options_only_reports_what_is_installed(self, monkeypatch):
        pod = self._pod(monkeypatch)
        result = m.handle_dut_flm("lab")
        assert result == {"installed": False}
        pod.resolve_flm_algo.assert_not_called()
        pod.install_flm_algo.assert_not_called()

    def test_device_alone_resolves_and_installs(self, monkeypatch):
        pod = self._pod(monkeypatch)
        result = m.handle_dut_flm("lab", device="nRF52840_xxAA")
        pod.resolve_flm_algo.assert_called_once_with(device="nRF52840_xxAA")
        pod.install_flm_algo.assert_called_once_with({"name": "resolved"})
        assert result == {"installed": True}

    def test_pack_reaches_resolve_flm_algo(self, monkeypatch):
        pod = self._pod(monkeypatch)
        m.handle_dut_flm("lab", pack="/path/to/x.pack")
        pod.resolve_flm_algo.assert_called_once_with(
            device=None, pack="/path/to/x.pack")

    def test_download_with_vendor_and_pack_name(self, monkeypatch):
        pod = self._pod(monkeypatch)
        m.handle_dut_flm("lab", download=True, vendor="NordicSemiconductor",
                         pack_name="nRF_DeviceFamilyPack")
        pod.resolve_flm_algo.assert_called_once_with(
            device=None, allow_download=True, vendor="NordicSemiconductor",
            pack_name="nRF_DeviceFamilyPack")

    def test_download_without_vendor_or_pack_name_omits_them(self, monkeypatch):
        pod = self._pod(monkeypatch)
        m.handle_dut_flm("lab", download=True)
        pod.resolve_flm_algo.assert_called_once_with(
            device=None, allow_download=True)

    def test_force_alone_still_resolves_and_installs(self, monkeypatch):
        pod = self._pod(monkeypatch)
        m.handle_dut_flm("lab", force=True)
        pod.resolve_flm_algo.assert_called_once_with(device=None)
        pod.install_flm_algo.assert_called_once()


class TestBusyNamesTheHolder:
    """The pod refuses a second REPL client with a line naming the holder, but
    the transport reports only the closed socket, so the name has to be asked
    for or it never reaches the agent that collided."""

    def _call(self, name, arguments):
        import asyncio
        import mcp.types as t
        srv = m.build_server()
        handler = srv.request_handlers[t.CallToolRequest]
        req = t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(name=name, arguments=arguments))
        return asyncio.run(handler(req)).root

    def _body(self, res):
        import json
        return json.loads(res.content[0].text)

    def test_a_busy_failure_carries_the_holder(self, monkeypatch):
        from pod.client import PodExecError

        def busy(*a, **k):
            raise PodExecError("exec", 1, "", "Connection reset by peer")

        fake = MagicMock()
        fake.repl_holder.return_value = (
            "annealage-pod: BUSY - REPL in use by another client, "
            "held by FD32::1:42232 for 12s")
        monkeypatch.setattr(m, "handle_pod_exec", busy)
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)

        body = self._body(self._call("pod_exec", {"label": "lab", "code": "x"}))
        assert body["kind"] == "busy"
        assert "FD32::1:42232" in body["held_by"]

    def test_an_unreachable_holder_probe_is_not_fatal(self, monkeypatch):
        from pod.client import PodExecError

        def busy(*a, **k):
            raise PodExecError("exec", 1, "", "Connection reset by peer")

        fake = MagicMock()
        fake.repl_holder.side_effect = OSError("no route")
        monkeypatch.setattr(m, "handle_pod_exec", busy)
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)

        body = self._body(self._call("pod_exec", {"label": "lab", "code": "x"}))
        assert body["kind"] == "busy"            # still classified
        assert body["held_by"] is None

    def test_a_real_fault_does_not_probe_the_repl(self, monkeypatch):
        """Probing takes the slot when it is free, so it must only run once the
        failure is already known to be contention."""
        from pod.client import PodExecError

        def broken(*a, **k):
            raise PodExecError("exec", 1, "", "could not enter raw repl")

        fake = MagicMock()
        fake.repl_holder.side_effect = AssertionError("probed on a real fault")
        monkeypatch.setattr(m, "handle_pod_exec", broken)
        monkeypatch.setattr(m, "_pod_for", lambda label: fake)

        body = self._body(self._call("pod_exec", {"label": "lab", "code": "x"}))
        assert body["kind"] == "pod_exec_failed"
        assert body["held_by"] is None
