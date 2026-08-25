"""CMSIS Device Family Pack lookup, host side.

A Device Family Pack (``.pack``) is a zip carrying a ``.pdsc`` XML description
plus the vendor's flash algorithms (``.FLM``). The pdsc names each device and,
per device, which algorithm covers which flash region and how much RAM the
algorithm may use. This module finds a pack for a device, reads that
description, and hands the chosen ``.FLM`` to :mod:`pod.flm` to become the algo
dict the pod runs.

The pod stores no flash algorithms. One is resolved per DUT here and installed
over the REPL, which is why pack resolution is a host concern.

Resolution order for a device, all offline unless downloading is asked for:

1. an explicit ``.pack`` or ``.FLM`` path supplied by the caller;
2. packs already in the local cache directory;
3. a download from the vendor index - only when ``allow_download=True``, since
   packs are large (tens to hundreds of MB) and fetching one reaches out to a
   third-party server.

Cache directory: ``$ANNEALAGE_POD_PACK_CACHE`` if set, else
``$XDG_CACHE_HOME/annealage-pod/cmsis-packs``, else
``~/.cache/annealage-pod/cmsis-packs``.

Public API
----------
algo_for_device(device, ...) -> dict
    The whole path: resolve a pack, pick the algorithm, emit the pod algo dict.

find_device(device, ...) -> (Pack, DeviceInfo)
    Locate a device in a local pack without building an algorithm.

Pack(path), Pack.device(name), DeviceInfo.flash_algorithm(), .ram_region()
    The pieces, for callers that want to choose themselves.

download_pack(vendor, name, version=None, ...) -> Path
index_entries(...) -> list of {vendor, name, version, url}
    Vendor index access. Both perform network I/O.
"""

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from pod import flm as _flm

DEFAULT_INDEX_URL = "https://www.keil.com/pack/index.pidx"

# Memory ids in a pdsc follow the CMSIS convention IROM<n> / IRAM<n>. Newer
# packs may instead carry an access string; writable memory is RAM.
_RAM_ID = re.compile(r"^IRAM\d*$", re.I)
_ROM_ID = re.compile(r"^IROM\d*$", re.I)


class PackError(Exception):
    """A pack could not be found, read, or does not describe the device."""


def cache_dir():
    """Directory holding downloaded packs; created on demand by the caller."""
    env = os.environ.get("ANNEALAGE_POD_PACK_CACHE")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "annealage-pod" / "cmsis-packs"


# ── pdsc model ───────────────────────────────────────────────────────────

class DeviceInfo:
    """One device from a pdsc, with the properties it inherits.

    Attributes:
        name: the Dname the pdsc declares.
        vendor: the pack's vendor.
        algorithms: [{file, start, size, ram_start, ram_size, default}].
        memories: [{id, access, start, size, default}].
    """

    def __init__(self, name, vendor, algorithms, memories):
        self.name = name
        self.vendor = vendor
        self.algorithms = algorithms
        self.memories = memories

    def __repr__(self):
        return "<DeviceInfo %s (%d algorithms)>" % (self.name,
                                                    len(self.algorithms))

    def flash_algorithm(self, addr=None):
        """Pick the algorithm to use, optionally the one covering addr.

        Prefers an algorithm whose region contains addr; otherwise the one the
        pack marks default; otherwise the only/first one.
        """
        if not self.algorithms:
            raise PackError("device %s declares no flash algorithm" % self.name)
        if addr is not None:
            covering = [a for a in self.algorithms
                        if a["start"] <= addr < a["start"] + a["size"]]
            if covering:
                return _prefer_default(covering)
        return _prefer_default(self.algorithms)

    def ram_region(self, algorithm=None):
        """RAM the algorithm may occupy, as (start, size).

        An algorithm's own RAMstart/RAMsize wins when the pack gives it; the
        vendor set those precisely so the algorithm does not collide with
        anything else the part needs at reset.
        """
        if algorithm and algorithm.get("ram_start") is not None:
            return algorithm["ram_start"], algorithm["ram_size"]
        rams = [m for m in self.memories if _is_ram(m)]
        if not rams:
            raise PackError("device %s declares no RAM region" % self.name)
        default = [m for m in rams if m.get("default")]
        chosen = (default or sorted(rams, key=lambda m: -m["size"]))[0]
        return chosen["start"], chosen["size"]

    def flash_regions(self):
        """Declared flash regions as [(start, size), ...]."""
        return [(m["start"], m["size"]) for m in self.memories if _is_rom(m)]


def _prefer_default(algorithms):
    for a in algorithms:
        if a.get("default"):
            return a
    return algorithms[0]


def _classify(mem):
    """"ram", "rom" or None for a pdsc memory element.

    Older packs identify memory by the CMSIS id tokens (IRAM1 / IROM1); newer
    ones drop the id and carry a free-form name plus an access string, where
    writable means RAM. The id wins when present because it is the unambiguous
    one; the name is only consulted if there is neither id nor access.
    """
    ident = mem.get("id")
    if ident:
        if _RAM_ID.match(ident):
            return "ram"
        if _ROM_ID.match(ident):
            return "rom"
    access = (mem.get("access") or "").lower()
    if access:
        return "ram" if "w" in access else "rom"
    name = mem.get("name") or ""
    if _RAM_ID.match(name):
        return "ram"
    if _ROM_ID.match(name):
        return "rom"
    return None


def _is_ram(mem):
    return _classify(mem) == "ram"


def _is_rom(mem):
    return _classify(mem) == "rom"


def _int(value):
    return None if value is None else int(value, 0)


def _normalise_path(path):
    """Pack-relative path from pdsc text: Windows separators, sometimes doubled."""
    return re.sub(r"[\\/]+", "/", path).lstrip("/")


def parse_pdsc(text):
    """Parse pdsc XML into {device name: DeviceInfo}.

    Device properties are declared at family, subFamily, device or variant
    level and inherit down the chain, so the walk carries an accumulated
    context and each device snapshots it.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PackError("pdsc is not valid XML: %s" % exc) from exc

    vendor = (root.findtext("vendor") or "").strip()
    devices = {}

    def _collect(node, algorithms, memories):
        # Inner levels refine what they inherit: a memory re-declared at a
        # deeper level (same id/name) replaces the outer one, which is how a
        # family declares a nominal region and each device gives its real size.
        # Algorithms accumulate instead - a device legitimately has several,
        # one per flash region - deduplicated on file and region.
        algorithms = list(algorithms)
        memories = dict(memories)
        for el in node:
            if el.tag == "algorithm":
                entry = {
                    "file": _normalise_path(el.get("name", "")),
                    "start": _int(el.get("start")) or 0,
                    "size": _int(el.get("size")) or 0,
                    "ram_start": _int(el.get("RAMstart")),
                    "ram_size": _int(el.get("RAMsize")),
                    "default": el.get("default") in ("1", "true"),
                }
                algorithms = [a for a in algorithms
                              if (a["file"], a["start"]) !=
                              (entry["file"], entry["start"])]
                algorithms.append(entry)
            elif el.tag == "memory":
                entry = {
                    "id": el.get("id"),
                    "name": el.get("name"),
                    "access": el.get("access"),
                    "start": _int(el.get("start")) or 0,
                    "size": _int(el.get("size")) or 0,
                    "default": el.get("default") in ("1", "true"),
                }
                memories[entry["id"] or entry["name"] or
                         ("@0x%08x" % entry["start"])] = entry
        return algorithms, memories

    def _walk(node, algorithms, memories):
        algorithms, memories = _collect(node, algorithms, memories)
        name = node.get("Dname") or node.get("Dvariant")
        if name:
            devices[name] = DeviceInfo(name, vendor, algorithms,
                                       list(memories.values()))
        for child in node:
            if child.tag in ("subFamily", "device", "variant"):
                _walk(child, algorithms, memories)

    for family in root.iter("family"):
        _walk(family, [], {})
    return devices


# ── pack container ───────────────────────────────────────────────────────

class Pack:
    """An opened Device Family Pack (a zip holding a pdsc and its algorithms)."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise PackError("no such pack: %s" % self.path)
        try:
            self._zip = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise PackError("%s is not a readable .pack (zip): %s"
                            % (self.path, exc)) from exc
        names = [n for n in self._zip.namelist() if n.lower().endswith(".pdsc")]
        if not names:
            raise PackError("%s contains no .pdsc description" % self.path)
        self.pdsc_name = names[0]
        self._devices = None

    def close(self):
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self, member):
        """Read a member by pack-relative path, case-insensitively.

        Pdsc files are authored on Windows and routinely disagree with the
        archive on case and separator.
        """
        member = _normalise_path(member)
        try:
            return self._zip.read(member)
        except KeyError:
            pass
        want = member.lower()
        for name in self._zip.namelist():
            if _normalise_path(name).lower() == want:
                return self._zip.read(name)
        raise PackError("%s is not in %s" % (member, self.path))

    @property
    def devices(self):
        if self._devices is None:
            self._devices = parse_pdsc(self._zip.read(self.pdsc_name))
        return self._devices

    def device(self, name):
        """Look up a device by name, case-insensitively."""
        devices = self.devices
        if name in devices:
            return devices[name]
        lowered = {k.lower(): v for k, v in devices.items()}
        if name.lower() in lowered:
            return lowered[name.lower()]
        raise PackError("%s does not describe device %r (has %d devices: %s%s)"
                        % (self.path.name, name, len(devices),
                           ", ".join(sorted(devices)[:5]),
                           ", ..." if len(devices) > 5 else ""))


def local_packs(cache=None):
    """Every .pack in the cache directory, newest-versioned first."""
    directory = Path(cache) if cache else cache_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.pack"), reverse=True)


def find_device(device, cache=None, packs=None):
    """Find a device in the local packs.

    Returns (Pack, DeviceInfo). Raises PackError if no local pack has it; the
    caller decides whether to download.
    """
    candidates = list(packs) if packs is not None else local_packs(cache)
    if not candidates:
        raise PackError(
            "no CMSIS packs cached in %s; supply a pack path or allow a "
            "download" % (Path(cache) if cache else cache_dir()))
    tried = []
    for path in candidates:
        try:
            pack = Pack(path)
        except PackError as exc:
            tried.append(str(exc))
            continue
        try:
            return pack, pack.device(device)
        except PackError:
            pack.close()
            tried.append("%s: no %s" % (path.name, device))
    raise PackError("no cached pack describes %r (checked %d: %s)"
                    % (device, len(candidates), "; ".join(tried[:5])))


# ── vendor index and download (network) ──────────────────────────────────

def index_entries(index_url=DEFAULT_INDEX_URL, timeout=30):
    """Fetch the vendor pack index. Performs network I/O.

    Returns [{vendor, name, version, url}], one per published pack.
    """
    from urllib.request import urlopen

    with urlopen(index_url, timeout=timeout) as resp:
        raw = resp.read()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise PackError("pack index is not valid XML: %s" % exc) from exc

    entries = []
    for el in root.iter("pdsc"):
        url, vendor, name = el.get("url"), el.get("vendor"), el.get("name")
        if url and vendor and name:
            entries.append({"vendor": vendor, "name": name,
                            "version": el.get("version"), "url": url})
    if not entries:
        raise PackError("pack index at %s listed no packs" % index_url)
    return entries


def download_pack(vendor, name, version=None, cache=None,
                  index_url=DEFAULT_INDEX_URL, timeout=300):
    """Download one vendor pack into the cache and return its path.

    Performs network I/O against the vendor's server. Skips the download if the
    file is already cached.
    """
    from urllib.request import urlopen

    entries = [e for e in index_entries(index_url)
               if e["vendor"] == vendor and e["name"] == name]
    if not entries:
        raise PackError("pack index has no %s.%s" % (vendor, name))
    entry = entries[0]
    version = version or entry["version"]
    if not version:
        raise PackError("pack index gives no version for %s.%s"
                        % (vendor, name))

    directory = Path(cache) if cache else cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("%s.%s.%s.pack" % (vendor, name, version))
    if target.exists():
        return target

    url = entry["url"].rstrip("/") + "/%s.%s.%s.pack" % (vendor, name, version)
    partial = target.with_suffix(".pack.part")
    with urlopen(url, timeout=timeout) as resp, open(partial, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    partial.replace(target)
    return target


# ── the whole path ───────────────────────────────────────────────────────

def algo_for_device(device, pack=None, cache=None, addr=None,
                    stack_size=None, page_buffer=None, ram=None,
                    allow_download=False, vendor=None, pack_name=None):
    """Resolve a flash algorithm for a device and return the pod's algo dict.

    Args:
        device: CMSIS device name, e.g. "nRF52840_xxAA". The pod registry's
            declared dut.target_family carries exactly this.
        pack: explicit .pack or .FLM path, bypassing cache and index. A .FLM
            needs an explicit ram=(start, size), since a bare algorithm file
            carries no RAM description.
        cache: override the pack cache directory.
        addr: prefer the algorithm covering this flash address.
        stack_size, page_buffer: algorithm RAM layout overrides.
        ram: (start, size) overriding what the pack declares.
        allow_download: permit fetching the pack from the vendor index.
        vendor, pack_name: which pack to download; required with
            allow_download when nothing local matches.

    Returns:
        The algo dict for annealage_pod.debug.ops.set_flm_algo().
    """
    kwargs = {}
    if stack_size is not None:
        kwargs["stack_size"] = stack_size
    if page_buffer is not None:
        kwargs["page_buffer"] = page_buffer

    # An explicit .FLM: no pack, so the caller must say where it may run.
    if pack and str(pack).lower().endswith(".flm"):
        if not ram:
            raise PackError(
                "a bare .FLM carries no RAM description; pass ram=(start, size)")
        image = _flm.parse_flm(Path(pack).read_bytes())
        return image.build_algo(ram[0], ram[1], name=device, **kwargs)

    if pack:
        container, info = Pack(pack), None
        info = container.device(device)
    else:
        try:
            container, info = find_device(device, cache)
        except PackError:
            if not (allow_download and vendor and pack_name):
                raise
            path = download_pack(vendor, pack_name, cache=cache)
            container = Pack(path)
            info = container.device(device)

    try:
        algorithm = info.flash_algorithm(addr)
        ram_start, ram_size = ram if ram else info.ram_region(algorithm)
        image = _flm.parse_flm(container.read(algorithm["file"]))
        return image.build_algo(ram_start, ram_size, name=device, **kwargs)
    finally:
        container.close()
