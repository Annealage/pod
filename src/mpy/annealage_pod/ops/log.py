# Annealage Pod: TCP log socket wrapper.
#
# Spec.md §6.1 says stdout is dup'd to UART0 always and to a TCP log
# socket when a client is connected. UART0 dup is automatic on
# ESP32-S3 with the CH340N bridge; this module only owns the optional
# TCP side.
#
# Implementation strategy: bind a TCP listener; on accept, set the
# accepted socket as a stdout dupterm via os.dupterm(), with the
# UART0 dupterm staying intact (MP supports up to 2 dupterms).

import os

try:
    import socket as _socket
except ImportError:
    _socket = None


_LOG_PORT_DEFAULT = 514  # syslog default; spec.md §5.3 lists it as optional


_listener = None
_client = None
_DUPTERM_SLOT = 1


def start_socket(port=_LOG_PORT_DEFAULT):
    """Start the TCP log listener on `port`. Returns the listening socket."""
    global _listener
    if _socket is None:
        return None
    if _listener is not None:
        return _listener
    addr = _socket.getaddrinfo("0.0.0.0", port)[0][-1]
    s = _socket.socket()
    s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    _listener = s
    return s


def accept_one():
    """Accept one client and dup stdout to it. Returns the client socket or None."""
    global _client
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


def stop():
    """Close the active log client and stop the listener."""
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
