# Annealage Pod: OTA update wrapper.
#
# Spec.md §4.3 wires this to ESP-IDF `esp_https_ota`. MicroPython does
# not expose esp_https_ota directly, so the production path needs a
# small C shim that calls esp_https_ota_simple under the hood. WS-H
# (plan/phase-2-parallel-implementation.md) owns the shim landing.
#
# Until that lands, this wrapper falls back to a pure-MicroPython
# slot-based update via `esp32.Partition`: stream the new image into
# the inactive OTA slot, validate, mark it bootable. This is slower
# than esp_https_ota and lacks rollback metadata; the C path is
# preferred. The pure-MP fallback is documented as
# `experimental_pure_mp=True` so callers know it is not the spec
# default.

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
    """Return the ESP-IDF wrapper module if the C shim is present."""
    try:
        import esp_https_ota  # type: ignore

        return esp_https_ota
    except ImportError:
        return None


def update(url, *, experimental_pure_mp=False, chunk_size=4096):
    """Fetch a firmware image from `url` and stage it for next boot.

    Default path: call the C shim wrapping esp_https_ota. If the
    shim is not present and `experimental_pure_mp` is True, stream
    the image into the inactive OTA partition via esp32.Partition.
    Returns True on success.
    """
    shim = _esp_https_ota()
    if shim is not None:
        update_fn = getattr(shim, "update", None) or getattr(shim, "ota_update", None)
        if update_fn is not None:
            return bool(update_fn(url))
        # TODO(WS-H): expose a documented update() in the C shim.
        print("annealage_pod.ops.ota: esp_https_ota shim present but no update() symbol")
    if not experimental_pure_mp:
        raise NotImplementedError(
            "annealage_pod.ops.ota.update: C esp_https_ota shim missing; "
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
    """Mark the running OTA image as valid; aborts rollback."""
    if _esp32 is None:
        return False
    try:
        _esp32.Partition.mark_app_valid_cancel_rollback()
        return True
    except (AttributeError, OSError):
        return False
