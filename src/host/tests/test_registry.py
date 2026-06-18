"""Tests for pod.registry - CRUD and reconcile against a tmp dir."""

import json
import os
import pytest
from pathlib import Path

from pod.registry import (
    load_registry,
    save_registry,
    get_pod,
    set_pod,
    update_pod,
    remove_pod,
    reconcile,
    reconcile_dut,
    _migrate_entry,
)
from pod.discovery import PodInfo


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Point POD_CONFIG_DIR at a fresh tmp directory for every test."""
    monkeypatch.setenv("POD_CONFIG_DIR", str(tmp_path))
    yield tmp_path


class TestLoadSave:
    def test_load_empty(self):
        registry = load_registry()
        assert registry["version"] == 2
        assert registry["pods"] == {}

    def test_save_and_reload(self):
        registry = load_registry()
        registry["pods"]["alpha"] = {"address": "1.2.3.4", "repl_port": 8266}
        save_registry(registry)
        loaded = load_registry()
        assert loaded["pods"]["alpha"]["address"] == "1.2.3.4"

    def test_file_ends_with_newline(self, tmp_path):
        registry = load_registry()
        save_registry(registry)
        content = (tmp_path / "pods.json").read_text()
        assert content.endswith("\n")

    def test_registry_dir_created(self, tmp_path):
        sub = tmp_path / "deep" / "dir"
        os.environ["POD_CONFIG_DIR"] = str(sub)
        load_registry()
        save_registry(load_registry())
        assert (sub / "pods.json").exists()
        os.environ["POD_CONFIG_DIR"] = str(tmp_path)


class TestGetSetRemove:
    def test_get_missing_returns_none(self):
        assert get_pod("nope") is None

    def test_set_then_get(self):
        set_pod("my-pod", {"address": "192.168.1.1", "repl_port": 8266})
        entry = get_pod("my-pod")
        assert entry is not None
        assert entry["address"] == "192.168.1.1"

    def test_set_overwrites(self):
        set_pod("my-pod", {"address": "10.0.0.1", "repl_port": 8266})
        set_pod("my-pod", {"address": "10.0.0.2", "repl_port": 8266})
        assert get_pod("my-pod")["address"] == "10.0.0.2"

    def test_remove_existing(self):
        set_pod("to-remove", {"address": "5.6.7.8", "repl_port": 8266})
        removed = remove_pod("to-remove")
        assert removed is True
        assert get_pod("to-remove") is None

    def test_remove_missing_returns_false(self):
        assert remove_pod("ghost") is False

    def test_multiple_pods(self):
        set_pod("pod-a", {"address": "1.1.1.1", "repl_port": 8266})
        set_pod("pod-b", {"address": "2.2.2.2", "repl_port": 8266})
        registry = load_registry()
        assert "pod-a" in registry["pods"]
        assert "pod-b" in registry["pods"]


class TestReconcile:
    def _make_pod_info(self, address="192.168.0.121"):
        return PodInfo(
            name="annealage-pod",
            address=address,
            port=8266,
            repl_port=8266,
            usbip_port=3240,
            uart_port=2000,
            gdb_port=3335,
            carrier_id="",
            mp_version="1.29.0.preview",
        )

    def test_reconcile_creates_entry(self):
        entry = reconcile("lab-pod", self._make_pod_info())
        assert entry["address"] == "192.168.0.121"
        assert entry["repl_port"] == 8266
        assert entry["usbip_port"] == 3240
        assert entry["uart_port"] == 2000
        assert entry["gdb_port"] == 3335
        assert entry["mp_version"] == "1.29.0.preview"

    def test_reconcile_updates_address(self):
        reconcile("lab-pod", self._make_pod_info("192.168.0.121"))
        entry = reconcile("lab-pod", self._make_pod_info("192.168.0.200"))
        assert entry["address"] == "192.168.0.200"

    def test_reconcile_preserves_notes(self):
        set_pod("lab-pod", {
            "address": "192.168.0.100",
            "repl_port": 8266,
            "notes": "keep this note",
        })
        entry = reconcile("lab-pod", self._make_pod_info())
        assert entry.get("notes") == "keep this note"

    def test_reconcile_sets_last_seen(self):
        entry = reconcile("lab-pod", self._make_pod_info())
        assert "last_seen" in entry
        assert entry["last_seen"]  # non-empty

    def test_reconcile_persisted(self):
        reconcile("lab-pod", self._make_pod_info())
        assert get_pod("lab-pod") is not None


class TestMigration:
    def test_v1_ipv4_address_becomes_addr4(self):
        e = {"address": "192.168.0.50", "repl_port": 8266}
        _migrate_entry(e)
        assert e["addr4"] == "192.168.0.50"
        assert e["addr6"] == []
        assert e["hostname"] is None
        assert e["address"] == "192.168.0.50"   # never dropped

    def test_v1_ipv6_address_becomes_addr6(self):
        e = {"address": "fd32::1"}
        _migrate_entry(e)
        assert e["addr6"] == ["fd32::1"]
        assert e["addr4"] is None

    def test_v1_hostname_address_becomes_hostname(self):
        e = {"address": "annealage-pod.local"}
        _migrate_entry(e)
        assert e["hostname"] == "annealage-pod.local"
        assert e["addr4"] is None and e["addr6"] == []

    def test_migration_idempotent(self):
        e = {"address": "192.168.0.50"}
        _migrate_entry(e)
        first = dict(e)
        _migrate_entry(e)
        assert e == first

    def test_load_migrates_and_bumps_version(self):
        save_registry({"version": 1, "pods": {
            "old": {"address": "192.168.0.9", "repl_port": 8266}}})
        reg = load_registry()
        assert reg["version"] == 2
        assert reg["pods"]["old"]["addr4"] == "192.168.0.9"


class TestReconcilePreserves:
    def test_reconcile_keeps_dut_and_fingerprint(self):
        set_pod("p", {"addr4": "192.168.0.9", "repl_port": 8266,
                      "fingerprint": "abcd1234", "dut": {"label": "nrf"}})
        info = PodInfo(name="p", hostname="p.local", addr4="192.168.0.9",
                       repl_port=8266, usbip_port=None, uart_port=None,
                       gdb_port=None)
        entry = reconcile("p", info)
        assert entry["fingerprint"] == "abcd1234"
        assert entry["dut"] == {"label": "nrf"}
        assert entry["hostname"] == "p.local"


class TestReconcileDut:
    LIVE = {"ok": True, "dpidr": 0x2BA01477, "ap_idr": 0x24770011,
            "cpuid": 0x410FC241, "rom_base": 0xE00FF003}

    def test_no_declared(self):
        r = reconcile_dut(None, self.LIVE)
        assert r["verdict"] == "NO_DECLARED"

    def test_match(self):
        declared = {"expected": {"dpidr": 0x2BA01477, "cpuid": 0x410FC241}}
        r = reconcile_dut(declared, self.LIVE)
        assert r["verdict"] == "MATCH"

    def test_mismatch(self):
        declared = {"expected": {"dpidr": 0x12345678}}
        r = reconcile_dut(declared, self.LIVE)
        assert r["verdict"] == "MISMATCH"
        assert r["fields"]["dpidr"]["match"] is False

    def test_cpuid_revision_ignored(self):
        # low nibble (revision) differs; compare must still MATCH on the part.
        declared = {"expected": {"cpuid": 0x410FC240}}
        r = reconcile_dut(declared, self.LIVE)
        assert r["verdict"] == "MATCH"

    def test_undeclared_ids(self):
        r = reconcile_dut({"label": "nrf"}, self.LIVE)
        assert r["verdict"] == "UNDECLARED"

    def test_no_live(self):
        declared = {"expected": {"dpidr": 0x2BA01477}}
        r = reconcile_dut(declared, {"ok": False, "err": "no SWD"})
        assert r["verdict"] == "NO_LIVE"
