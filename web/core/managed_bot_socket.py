"""Single-endpoint socket handoff for untrusted managed national bots.

The trusted host opens the one TCP connection a bot is authorized to use, then
passes that descriptor through Bubblewrap into an otherwise isolated network
namespace.  The bootstrap adapts the historical ``socket.create_connection``
entry contract without exposing the host network namespace.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import stat


PRECONNECTED_FD_ENV = "POK_PRECONNECTED_SOCKET_FD"
PRECONNECTED_HOST_ENV = "POK_PRECONNECTED_SOCKET_HOST"
PRECONNECTED_PORT_ENV = "POK_PRECONNECTED_SOCKET_PORT"


SANDBOX_BOOTSTRAP = r"""
import os
import runpy
import socket
import sys
import _thread

_pok_fd = int(os.environ.pop("POK_PRECONNECTED_SOCKET_FD"))
_pok_host = os.environ.pop("POK_PRECONNECTED_SOCKET_HOST")
_pok_port = int(os.environ.pop("POK_PRECONNECTED_SOCKET_PORT"))
os.set_inheritable(_pok_fd, False)
_pok_fd_lock = _thread.allocate_lock()

def _pok_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                           source_address=None, *, all_errors=False):
    global _pok_fd
    try:
        requested_host, requested_port = address
        requested_port = int(requested_port)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OSError("managed bot endpoint is invalid") from exc
    if str(requested_host) != _pok_host or requested_port != _pok_port:
        raise OSError("managed bot requested an unauthorized endpoint")
    if source_address is not None:
        raise OSError("managed bot source-address override is forbidden")
    with _pok_fd_lock:
        if _pok_fd is None:
            raise OSError("managed bot preconnected socket was already consumed")
        descriptor = _pok_fd
        _pok_fd = None
    connected = socket.socket(fileno=descriptor)
    effective_timeout = (
        socket.getdefaulttimeout()
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT
        else timeout
    )
    connected.settimeout(effective_timeout)
    return connected

socket.create_connection = _pok_create_connection
entry = sys.argv[1]
sys.path.insert(0, "/bot")
sys.argv = [entry] + sys.argv[2:]
runpy.run_path(entry, run_name="__main__")
"""


def require_loopback_endpoint(host: str, port: int) -> tuple[str, int]:
    try:
        address = ipaddress.ip_address(str(host))
    except ValueError as exc:
        raise ValueError("managed bot endpoint must be a literal IP address") from exc
    if not address.is_loopback:
        raise ValueError("managed bot endpoint must be loopback")
    normalized_port = int(port)
    if not 1 <= normalized_port <= 65_535:
        raise ValueError("managed bot endpoint port is invalid")
    return str(address), normalized_port


def require_socket_fd(descriptor: int) -> int:
    value = int(descriptor)
    if value < 3:
        raise ValueError("managed bot socket descriptor must not replace stdio")
    try:
        metadata = os.fstat(value)
    except OSError as exc:
        raise ValueError("managed bot socket descriptor is unavailable") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise ValueError("managed bot descriptor is not a socket")
    return value


def endpoint_environment(descriptor: int, host: str, port: int) -> dict[str, str]:
    value = require_socket_fd(descriptor)
    normalized_host, normalized_port = require_loopback_endpoint(host, port)
    return {
        PRECONNECTED_FD_ENV: str(value),
        PRECONNECTED_HOST_ENV: normalized_host,
        PRECONNECTED_PORT_ENV: str(normalized_port),
    }


def connect_managed_endpoint(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
) -> socket.socket:
    normalized_host, normalized_port = require_loopback_endpoint(host, port)
    return socket.create_connection(
        (normalized_host, normalized_port),
        timeout=max(0.1, float(timeout)),
    )
