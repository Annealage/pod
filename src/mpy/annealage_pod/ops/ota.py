# Annealage Pod: OTA update wrapper.
#
# Spec.md §4.3 wires this to ESP-IDF `esp_https_ota`. WS-H provides
# a small C shim (`ops_ota`) that wraps esp_https_ota for synchronous
# use from the MP main task. WS-E left a pure-MP fallback for the
# Unix port and for environments that lack the C shim; this module
# prefers the C shim when present and falls back transparently.
#
# Public surface (callable from REPL or from boot.py):
#   update(url, cert_pem=None, *, experimental_pure_mp=False)
#   mark_valid()  /  mark_app_valid()  (alias)

try:
    import ops_ota as _ops_ota_c
except ImportError:
    _ops_ota_c = None

try:
    import urequests as _requests  # noqa: F401
except ImportError:
    try:
        import requests as _requests
    except ImportError:
        _requests = None

try:
    import esp32 as _esp32
except ImportError:
    _esp32 = None


def _esp_https_ota():
    """Return the C shim module if present, else None.

    Kept as a function for backward compatibility with the WS-E
    placeholder; tests may monkeypatch this to inject a fake.
    """
    return _ops_ota_c


def update(url, cert_pem=None, *, experimental_pure_mp=False, chunk_size=4096):
    """Fetch a firmware image from `url` and stage it for next boot.

    Default path: call the C shim wrapping esp_https_ota.
    Synchronous: blocks the calling task until the OTA finishes.
    Returns True on success; raises OSError(esp_err_t) on IDF
    failure.

    If the C shim is unavailable and `experimental_pure_mp` is True,
    streams the image into the inactive OTA partition via
    esp32.Partition. This is the WS-E fallback retained for the Unix
    port and for low-spec dev boards.
    """
    shim = _esp_https_ota()
    if shim is not None:
        update_fn = getattr(shim, "update", None) or getattr(shim, "ota_update", None)
        if update_fn is not None:
            return bool(update_fn(url, cert_pem))
        # Shim is loaded but missing update(); this should not
        # happen with the WS-H build but we keep the diagnostic.
        print("annealage_pod.ops.ota: ops_ota shim present but no update() symbol")
    if not experimental_pure_mp:
        raise NotImplementedError(
            "annealage_pod.ops.ota.update: C ops_ota shim missing; "
            "pass experimental_pure_mp=True to use the slow MP fallback"
        )
    return _pure_mp_update(url, chunk_size=chunk_size)


def _pure_mp_update(url, chunk_size=4096):
    if _requests is None:
        raise RuntimeError("annealage_pod.ops.ota: no urequests/requests module available")
    if _esp32 is None:
        raise RuntimeError("annealage_pod.ops.ota: esp32 module not available; not on ESP32 port")
    next_part = _esp32.Partition(_esp32.Partition.RUNNING).get_next_update()
    resp = _requests.get(url)
    try:
        offset = 0
        block_size = next_part.ioctl(5)  # block size
        if block_size <= 0:
            block_size = chunk_size
        buf = bytearray(block_size)
        view = memoryview(buf)
        rem = 0
        while True:
            chunk = resp.raw.read(chunk_size) if hasattr(resp, "raw") else resp.content[offset : offset + chunk_size]
            if not chunk:
                break
            n = len(chunk)
            view[rem : rem + n] = chunk
            rem += n
            while rem >= block_size:
                next_part.writeblocks(offset // block_size, view[:block_size])
                view[: rem - block_size] = view[block_size:rem]
                rem -= block_size
                offset += block_size
        if rem:
            # Pad the final block to block_size.
            for i in range(rem, block_size):
                buf[i] = 0xFF
            next_part.writeblocks(offset // block_size, buf)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    next_part.set_boot()
    return True


def mark_valid():
    """Mark the running OTA image as valid; aborts rollback.

    Prefers the C shim's `mark_app_valid`. Falls back to
    `esp32.Partition.mark_app_valid_cancel_rollback()` on the
    pure-MP path. Returns True on success, False otherwise.
    """
    shim = _esp_https_ota()
    if shim is not None and hasattr(shim, "mark_app_valid"):
        try:
            return bool(shim.mark_app_valid())
        except OSError:
            return False
    if _esp32 is None:
        return False
    try:
        _esp32.Partition.mark_app_valid_cancel_rollback()
        return True
    except (AttributeError, OSError):
        return False


# Alias matching the C-shim name so callers that import either
# spelling work uniformly.
mark_app_valid = mark_valid
