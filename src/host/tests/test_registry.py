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
    remove_pod,
    reconcile,
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
        assert registry["version"] == 1
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
            carrier_id="",
            mp_version="1.29.0.preview",
        )

    def test_reconcile_creates_entry(self):
        entry = reconcile("lab-pod", self._make_pod_info())
        assert entry["address"] == "192.168.0.121"
        assert entry["repl_port"] == 8266
        assert entry["usbip_port"] == 3240
        assert entry["uart_port"] == 2000
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
