"""Locks down the MCP/CLI surface contract (docs/pod/plan/mcp-surface.md):
exactly 27 namespaced MCP tools with one dispatch branch each, the
multiplexed tools (dut_reg, dut_mem, dut_link, bench_device) routing on
their read/write/action/bus arguments, and the CLI's nested `pod dut <verb>`
/ `pod bench <verb>` tree, with nothing outside that contract reachable on
either surface."""

import inspect
import re
import sys
from functools import lru_cache

import pytest
from unittest.mock import MagicMock

import pod.mcp_server as m
from pod.cli import main


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
    "dut_gdb", "dut_flash", "dut_erase", "dut_reset", "dut_link",
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
    """The tool list is exactly the 27 named tools, no more and no fewer."""

    def test_tool_list_has_no_duplicates(self):
        names = _tool_list_names()
        assert len(names) == len(set(names))

    def test_tool_list_is_exactly_the_27_named_tools(self):
        assert set(_tool_list_names()) == EXPECTED_TOOLS
        assert len(_tool_list_names()) == 27


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
        fake_pod.reprobe_dut.assert_called_once_with()
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

    def test_down_ignores_bus_and_sweeps_by_default(self, monkeypatch):
        fake_pod = MagicMock()
        fake_pod.peripheral_release.return_value = {"ok": True}
        self._patch(monkeypatch, fake_pod)
        result = m.handle_bench_device("lab", bus=None, action="down")
        fake_pod.peripheral_release.assert_called_once_with(name="*")
        assert result["ok"] is True

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


class TestSessionSubjectIsNotConfused:
    """Sessions are keyed by pod label, so a pod session and a DUT session
    contend for the same key. The open path must refuse the mismatch rather
    than hand back whichever session happens to be held."""

    def _held(self, monkeypatch, device):
        session = MagicMock()
        session.running = True
        monkeypatch.setitem(
            m._REPL_SESSIONS, "lab",
            {"session": session, "log_path": "/tmp/x.log", "device": device})
        return session

    def test_dut_open_refuses_when_a_pod_session_is_held(self, monkeypatch):
        self._held(monkeypatch, None)
        with pytest.raises(ValueError, match="already holds a session"):
            m.handle_dut_open("lab", device="/dev/ttyACM0")

    def test_pod_open_refuses_when_a_dut_session_is_held(self, monkeypatch):
        self._held(monkeypatch, "/dev/ttyACM0")
        with pytest.raises(ValueError, match="already holds a session"):
            m.handle_pod_open("lab")

    def test_dut_open_refuses_a_different_device(self, monkeypatch):
        self._held(monkeypatch, "/dev/ttyACM0")
        with pytest.raises(ValueError, match="already holds a session"):
            m.handle_dut_open("lab", device="/dev/ttyACM1")


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
    """Every public handle_* function is one of the 27 tools and every tool has
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
