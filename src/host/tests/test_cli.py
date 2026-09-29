"""Tests for pod.cli - invoke main() with synthetic argv and POD_CONFIG_DIR=tmp."""

import json
import sys
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock

import pod.cli as cli
from pod.cli import main


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Route registry I/O to a fresh tmp directory for every test."""
    monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
    yield tmp_path


class TestListEmpty:
    def test_list_empty_registry_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        ret = main()
        assert ret in (0, None)

    def test_list_empty_registry_no_output(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert out == ""

    def test_list_empty_registry_json_emits_empty_object(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "list", "--json"])
        main()
        out, _ = capsys.readouterr()
        assert out.strip() == "{}"


class TestListFormat:
    def _seed(self):
        from pod.registry import set_pod
        set_pod("has-dut", {
            "hostname": "has-dut.local", "addr4": "192.168.0.10", "addr6": [],
            "repl_port": 8266, "fingerprint": "abcd1234ef",
            "mp_version": "1.29.0.preview",
            "dut": {"target_family": "nRF52840_xxAA", "label": "lab-nrf"},
        })
        set_pod("no-dut", {
            "hostname": "no-dut.local", "addr4": None, "addr6": [],
            "repl_port": 8266, "fingerprint": None, "mp_version": "",
        })

    def test_table_leads_with_dut_column(self, monkeypatch, capsys):
        self._seed()
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        lines = out.splitlines()
        assert lines[0].split()[0] == "DUT"
        # Data rows: first whitespace-delimited field is the DUT column.
        data = {line.split()[0]: line for line in lines[1:]}
        assert "nRF52840_xxAA" in data
        assert "-" in data  # no-dut row's leading column

    def test_table_columns_are_aligned(self, monkeypatch, capsys):
        self._seed()
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        lines = out.splitlines()
        # Every LABEL column starts at the same offset across all rows.
        offsets = {line.index("has-dut") if "has-dut" in line else
                   line.index("no-dut") if "no-dut" in line else
                   line.index("LABEL")
                   for line in lines}
        assert len(offsets) == 1

    def test_json_flag_emits_full_registry_entries(self, monkeypatch, capsys):
        self._seed()
        monkeypatch.setattr(sys, "argv", ["pod", "list", "--json"])
        main()
        out, _ = capsys.readouterr()
        parsed = json.loads(out)
        assert set(parsed) == {"has-dut", "no-dut"}
        assert parsed["has-dut"]["dut"]["target_family"] == "nRF52840_xxAA"
        assert parsed["has-dut"]["fingerprint"] == "abcd1234ef"


class TestRegisterAndInfo:
    def _register(self, monkeypatch, label="test-pod", address="192.168.0.121", force=False):
        argv = [
            "pod", "register", label,
            "--address", address,
            "--repl-port", "8266",
            "--usbip-port", "3240",
            "--uart-port", "2000",
            "--carrier-id", "proto-v1",
            "--mp-version", "1.29.0.preview",
            "--no-probe",
        ]
        if force:
            argv.append("--force")
        monkeypatch.setattr(sys, "argv", argv)
        return main()

    def test_register_exits_zero(self, monkeypatch, capsys):
        ret = self._register(monkeypatch)
        assert ret in (0, None)

    def test_register_prints_confirmation(self, monkeypatch, capsys):
        self._register(monkeypatch)
        out, _ = capsys.readouterr()
        assert "test-pod" in out

    def test_register_then_list(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert "test-pod" in out

    def test_register_then_info(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        ret = main()
        assert ret in (0, None)
        out, _ = capsys.readouterr()
        assert "192.168.0.121" in out
        assert "8266" in out

    def test_info_shows_mp_version(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "1.29.0.preview" in out

    def test_info_shows_carrier_id(self, monkeypatch, capsys):
        self._register(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "proto-v1" in out

    def test_info_missing_label_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "info", "no-such-pod"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code != 0

    def test_duplicate_register_fails(self, monkeypatch, capsys):
        self._register(monkeypatch)
        capsys.readouterr()
        ret = self._register(monkeypatch)
        assert ret == 1

    def test_duplicate_register_force_succeeds(self, monkeypatch, capsys):
        self._register(monkeypatch)
        capsys.readouterr()
        ret = self._register(monkeypatch, address="10.0.0.99", force=True)
        assert ret in (0, None)
        # Verify new address was stored
        monkeypatch.setattr(sys, "argv", ["pod", "info", "test-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "10.0.0.99" in out

    def test_force_register_preserves_dut_and_notes(self, monkeypatch):
        # Seed an entry with a DUT block + notes + fingerprint, then re-register
        # the same label with a new address and no --dut flags.
        from pod.registry import set_pod, get_pod
        set_pod("keep-pod", {
            "addr4": "192.168.0.5", "addr6": [], "repl_port": 8266,
            "fingerprint": "abcd1234",
            "dut": {"label": "nrf", "expected": {"dpidr": 0x2BA01477}},
            "notes": "bench 3",
        })
        ret = self._register(monkeypatch, label="keep-pod",
                             address="192.168.0.77", force=True)
        assert ret in (0, None)
        e = get_pod("keep-pod")
        assert e["addr4"] == "192.168.0.77"          # handle refreshed
        assert e["dut"] == {"label": "nrf", "expected": {"dpidr": 0x2BA01477}}
        assert e["notes"] == "bench 3"
        assert e["fingerprint"] == "abcd1234"        # re-probe failed -> kept


class TestUnregister:
    def test_unregister_existing(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "rm-pod",
            "--address", "1.2.3.4",
        ])
        main()
        capsys.readouterr()  # drain register output
        monkeypatch.setattr(sys, "argv", ["pod", "unregister", "rm-pod"])
        ret = main()
        assert ret in (0, None)
        capsys.readouterr()  # drain unregister output
        # Should no longer appear in list
        monkeypatch.setattr(sys, "argv", ["pod", "list"])
        main()
        out, _ = capsys.readouterr()
        assert "rm-pod" not in out

    def test_unregister_missing_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod", "unregister", "ghost"])
        ret = main()
        assert ret == 1


class TestFlashReset:
    # flash/reset are wired to the on-pod debug stack; with an unknown label
    # they resolve-then-fail (exit 1) rather than printing a stub message.
    def test_flash_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "flash", "nope", "fw.bin"])
        with pytest.raises(SystemExit):
            main()

    def test_reset_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "reset", "nope"])
        with pytest.raises(SystemExit):
            main()


class TestReprobe:
    def test_reprobe_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "link", "nope", "reprobe"])
        with pytest.raises(SystemExit):
            main()

    def test_reprobe_calls_reprobe_dut(self, monkeypatch):
        monkeypatch.setattr(cli, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        fake_pod = MagicMock()
        fake_pod.reprobe_dut.return_value = {"ok": True, "mounted": 2}
        monkeypatch.setattr(cli, "Pod",
                            SimpleNamespace(from_entry=lambda entry: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "link", "rp", "reprobe"])
        assert main() == 0
        fake_pod.reprobe_dut.assert_called_once_with(force=False)

    def test_reprobe_exit_1_when_not_ok(self, monkeypatch):
        monkeypatch.setattr(cli, "get_pod", lambda label: {"addr4": "10.0.0.1"})
        fake_pod = MagicMock()
        fake_pod.reprobe_dut.return_value = {"ok": False, "err": "no reprobe verb"}
        monkeypatch.setattr(cli, "Pod",
                            SimpleNamespace(from_entry=lambda entry: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "link", "rp", "reprobe"])
        assert main() == 1


class TestRegisterGdbPort:
    def test_register_with_gdb_port_then_info(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "g-pod",
            "--address", "192.168.0.50",
            "--gdb-port", "4444",
        ])
        main()
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["pod", "info", "g-pod"])
        main()
        out, _ = capsys.readouterr()
        assert "4444" in out

    def test_register_default_gdb_port(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "pod", "register", "g-pod2",
            "--address", "192.168.0.51",
        ])
        main()
        capsys.readouterr()
        monkeypatch.setattr(sys, "argv", ["pod", "info", "g-pod2"])
        main()
        out, _ = capsys.readouterr()
        assert "3335" in out


class TestGdbCommand:
    def _register(self, monkeypatch, capsys, label="gdb-pod", gdb_port=None):
        argv = ["pod", "register", label, "--address", "192.168.0.60"]
        if gdb_port is not None:
            argv += ["--gdb-port", str(gdb_port)]
        monkeypatch.setattr(sys, "argv", argv)
        main()
        capsys.readouterr()

    def test_gdb_unknown_label_errors(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "gdb", "nope"])
        with pytest.raises(SystemExit):
            main()

    def test_gdb_calls_endpoint_with_defaults(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys)
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 5005)
        monkeypatch.setattr(cli, "Pod", SimpleNamespace(from_entry=lambda entry: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "gdb", "gdb-pod"])
        ret = main()
        assert ret in (0, None)
        fake_pod.gdb_endpoint.assert_called_once()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["listen_port"] == 0
        assert kwargs["gdb_port"] == 3335
        assert kwargs["reset_halt"] is True
        assert kwargs["resume_window_ms"] == 200

    def test_gdb_uses_registry_gdb_port(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys, label="gdb-pod3", gdb_port=4321)
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 6006)
        monkeypatch.setattr(cli, "Pod", SimpleNamespace(from_entry=lambda entry: fake_pod))
        monkeypatch.setattr(sys, "argv", ["pod", "dut", "gdb", "gdb-pod3"])
        main()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["gdb_port"] == 4321

    def test_gdb_flags_passed_through(self, monkeypatch, capsys):
        self._register(monkeypatch, capsys, label="gdb-pod4")
        fake_pod = MagicMock()
        fake_pod.gdb_endpoint.return_value = ("127.0.0.1", 7007)
        monkeypatch.setattr(cli, "Pod", SimpleNamespace(from_entry=lambda entry: fake_pod))
        monkeypatch.setattr(sys, "argv", [
            "pod", "dut", "gdb", "gdb-pod4",
            "--listen-port", "9999",
            "--gdb-port", "3336",
            "--no-reset-halt",
            "--resume-window-ms", "100",
        ])
        main()
        kwargs = fake_pod.gdb_endpoint.call_args.kwargs
        assert kwargs["listen_port"] == 9999
        assert kwargs["gdb_port"] == 3336
        assert kwargs["reset_halt"] is False
        assert kwargs["resume_window_ms"] == 100


class TestNoSubcommand:
    def test_no_subcommand_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["pod"])
        ret = main()
        assert ret in (1, None) or ret != 0


class TestCarryOver:
    """Unit tests for enroll.carry_over field preservation on re-register."""

    def test_explicit_dut_fields_merge_over_existing(self):
        from pod.enroll import carry_over
        existing = {"dut": {"label": "nrf", "target_family": "nRF52840_xxAA",
                            "expected": {"dpidr": 1}}}
        entry = {"dut": {"notes": "swapped probe"}}   # only a new field set
        carry_over(existing, entry)
        assert entry["dut"]["label"] == "nrf"          # old fields kept
        assert entry["dut"]["expected"] == {"dpidr": 1}
        assert entry["dut"]["notes"] == "swapped probe"   # new merged in

    def test_new_fingerprint_wins_over_old(self):
        from pod.enroll import carry_over
        entry = {"fingerprint": "new"}
        carry_over({"fingerprint": "old"}, entry)
        assert entry["fingerprint"] == "new"

    def test_no_existing_is_noop(self):
        from pod.enroll import carry_over
        entry = {"addr4": "1.2.3.4"}
        carry_over(None, entry)
        assert entry == {"addr4": "1.2.3.4"}


class TestReRegisterGuards:
    """A re-register refreshes one board's handles; it never swaps boards."""

    @staticmethod
    def _pod(host):
        from types import SimpleNamespace
        return SimpleNamespace(hostname=host + ".local", name=host + "._x._tcp")

    def test_lone_pod_taken_for_a_new_label(self):
        from pod.enroll import match_discovered
        only = self._pod("annealage-pod")
        assert match_discovered([only], "bench-pod") is only

    def test_lone_pod_not_taken_for_a_registered_label(self):
        # The named pod is rebooting; the one still advertising is another board.
        from pod.enroll import match_discovered
        other = self._pod("annealage-pod")
        assert match_discovered([other], "annealage-pod-8b97a",
                                registered=True) is None

    def test_registered_label_still_matches_by_name(self):
        from pod.enroll import match_discovered
        want = self._pod("annealage-pod-8b97a")
        assert match_discovered([self._pod("annealage-pod"), want],
                                "annealage-pod-8b97a", registered=True) is want

    def test_different_fingerprint_refused(self):
        from pod.enroll import check_same_board
        with pytest.raises(ValueError, match="unregister"):
            check_same_board("pod-b", {"fingerprint": "8e49"},
                             {"fingerprint": "d83a"})

    def test_same_or_unknown_fingerprint_allowed(self):
        from pod.enroll import check_same_board
        check_same_board("pod-b", {"fingerprint": "8e49"}, {"fingerprint": "8e49"})
        check_same_board("pod-b", {"fingerprint": "8e49"}, {})  # probe failed
        check_same_board("pod-b", None, {"fingerprint": "8e49"})


class TestInstallUdev:
    def test_rule_scopes_to_vhci_and_ignores_mm(self):
        text = cli._udev_rule_text([])
        assert 'DRIVERS=="vhci_hcd"' in text
        assert 'ENV{ID_MM_DEVICE_IGNORE}="1"' in text
        assert "idVendor" not in text          # no per-VID line without --vid

    def test_rule_adds_lowercased_vid_fallback(self):
        text = cli._udev_rule_text(["F055"])
        assert 'ATTR{idVendor}=="f055"' in text

    def test_print_only_emits_and_writes_nothing(self, monkeypatch, capsys, tmp_path):
        target = tmp_path / "rule.rules"
        monkeypatch.setattr(sys, "argv",
                            ["pod", "install-udev", "--print", "--path", str(target)])
        rc = main()
        assert rc == 0
        assert "vhci_hcd" in capsys.readouterr().out
        assert not target.exists()             # --print does not write

    def test_writes_rule_and_reloads(self, monkeypatch, capsys, tmp_path):
        import subprocess
        target = tmp_path / "rule.rules"
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)  # stub udevadm
        monkeypatch.setattr(sys, "argv",
                            ["pod", "install-udev", "--path", str(target), "--vid", "f055"])
        rc = main()
        assert rc == 0
        body = target.read_text()
        assert 'ENV{ID_MM_DEVICE_IGNORE}="1"' in body
        assert 'ATTR{idVendor}=="f055"' in body
