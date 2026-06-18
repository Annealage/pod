# Annealage Pod: TCP log socket wrapper.
#
# Spec §6.1: stdout dup'd to UART0 (always) and to a TCP log socket
# when a client is connected. WS-H provides a C shim (`ops_log`)
# that hooks `esp_log_set_vprintf` and listens on a TCP port; the
# shim mirrors ESP_LOGx + stdout to both UART0 (chained vprintf)
# and the connected TCP client.
#
# WS-E left a pure-MP fallback using `os.dupterm()` over a manually
# accepted socket. The dupterm path covers MP `print()` only;
# ESP_LOGx output coming from C-side IDF components does not flow
# through MP's stdout. The C shim is therefore the production path
# on the ESP32 build. This wrapper prefers the shim and falls back
# to the dupterm path on the Unix port.

import os

try:
    import ops_log as _ops_log_c
except ImportError:
    _ops_log_c = None

try:
    import socket as _socket
except ImportError:
    _socket = None


_LOG_PORT_DEFAULT = 514  # syslog default; spec.md §5.3 lists it as optional


# Pure-MP fallback state. Only populated when _ops_log_c is None.
_listener = None
_client = None
_DUPTERM_SLOT = 1


def start(port=_LOG_PORT_DEFAULT, core=-1):
    """Start the TCP log fan-out on `port`.

    On the ESP32 build with the C shim available, this hooks
    esp_log_set_vprintf and runs an accept task on the chosen
    `core`. On Unix or builds lacking the shim, falls back to the
    pure-MP dupterm path (single client, MP-stdout only).

    Returns the C shim module on the shim path; the MP listener
    socket on the fallback path; or None if no transport is
    available.
    """
    if _ops_log_c is not None:
        _ops_log_c.start(port=port, core=core)
        return _ops_log_c
    return _start_socket_fallback(port)


def stop():
    """Stop the listener, drop the active client, restore vprintf hook."""
    if _ops_log_c is not None:
        _ops_log_c.stop()
        return True
    return _stop_fallback()


def client_count():
    """Return 0 if no client is attached, 1 otherwise."""
    if _ops_log_c is not None:
        return int(_ops_log_c.client_count())
    return 1 if _client is not None else 0


# --- Pure-MP fallback ---------------------------------------------------

def _start_socket_fallback(port):
    """Bind a TCP listener; one MP-side dupterm session at a time."""
    global _listener
    if _socket is None:
        return None
    if _listener is not None:
        return _listener
    # AF_INET6 + "::" = dual-stack (v4+v6) via modlwip's listen() promotion.
    s = _socket.socket(_socket.AF_INET6)
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(("::", port))
    s.listen(1)
    _listener = s
    return s


def accept_one():
    """Accept one client and dup MP stdout to it (fallback path only).

    Available only when running without the C shim. Returns the
    client socket, or None if the fallback listener is not running.
    """
    global _client
    if _ops_log_c is not None:
        return None
    if _listener is None:
        return None
    cli, _ = _listener.accept()
    if _client is not None:
        try:
            os.dupterm(None, _DUPTERM_SLOT)
            _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client = cli
    try:
        os.dupterm(cli, _DUPTERM_SLOT)
    except (TypeError, OSError) as exc:
        # Older MP signatures don't take the slot argument. Fall back.
        try:
            os.dupterm(cli)
        except Exception:
            print("annealage_pod.ops.log: os.dupterm failed: {!r}".format(exc))
    return cli


def _stop_fallback():
    """Tear down the pure-MP listener and any active client."""
    global _listener, _client
    if _client is not None:
        try:
            os.dupterm(None, _DUPTERM_SLOT)
        except Exception:  # noqa: BLE001
            pass
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
        _client = None
    if _listener is not None:
        try:
            _listener.close()
        except Exception:  # noqa: BLE001
            pass
        _listener = None
    return True


# Backward-compat aliases used by WS-E's earlier callers.
start_socket = start
