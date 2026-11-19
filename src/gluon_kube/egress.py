# SPDX-License-Identifier: MIT

"""The node's HTTP proxy, the cluster's way out (GK-5.3; ADR 0030 decision 3).

Pods have no route beyond the node: Kubernetes pods reach the internet through the
node's NAT, which needs iptables the sandbox doesn't have. Containers of images that
fetch from the internet (`curlimages/curl`: the `fetch-jmx-exporter` init container)
get `HTTPS_PROXY`/`HTTP_PROXY` pointing here (a difference their recipe states). This
proxy takes CONNECT and absolute-form HTTP requests: a cluster name (a Service, in any
of its /etc/hosts forms) or a cluster address is connected directly from the node, as
the pod would have; anything else goes out through the supervisor's relay.
"""

import contextlib
import ipaddress
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from gluon_kube.pods import NODE_IP, POD_NETWORK, SERVICE_NETWORK
from gluon_kube.proxy import CONNECT_TIMEOUT_S, splice

PROXY_PORT = 3128
"""controller.EGRESS_PROXY's port."""
HEAD_LIMIT = 65536


class EgressError(Exception):
    """An upstream that couldn't be reached; the message says why."""


def read_head(connection: socket.socket) -> tuple[bytes, bytes]:
    """An HTTP request's head (through the blank line) and what came after it."""
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > HEAD_LIMIT:
            raise EgressError("request head too long")
        chunk = connection.recv(4096)
        if not chunk:
            raise EgressError("connection closed before the request head ended")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def target_of(head: bytes) -> tuple[str, str, int, bytes]:
    """(method, host, port, the head to send upstream) from a proxy request: CONNECT's
    authority, or an absolute URI rewritten to origin form."""
    request_line, _, headers = head.partition(b"\r\n")
    try:
        method, uri, version = request_line.decode("latin-1").split(" ")
    except ValueError:
        raise EgressError(f"bad request line {request_line!r}") from None
    if method == "CONNECT":
        host, _, port = uri.rpartition(":")
        return method, host.strip("[]"), int(port), b""
    parts = urlsplit(uri)
    if parts.scheme != "http" or not parts.hostname:
        raise EgressError(f"only http:// and CONNECT are proxied, not {uri}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    line = f"{method} {path} {version}".encode("latin-1")
    # One request per connection: a client may send its next request, for another host,
    # on the same proxy connection, and the upstream is chosen once.
    kept = [
        h
        for h in headers.split(b"\r\n")
        if h
        and h.split(b":", 1)[0].strip().lower()
        not in (b"connection", b"proxy-connection", b"keep-alive")
    ]
    head = b"\r\n".join([line, *kept, b"Connection: close", b"", b""])
    return method, parts.hostname, parts.port or 80, head


def in_cluster(host: str) -> bool:
    """An address on the pod or Service network."""
    with contextlib.suppress(ValueError):
        address = ipaddress.ip_address(host)
        return address in POD_NETWORK or address in SERVICE_NETWORK
    return False


class HttpProxy:
    """An HTTP proxy (CONNECT and absolute-form requests) on `address`; subclasses say how
    to reach a host (`open`)."""

    name = "gluon-kube proxy"

    def __init__(self, address: tuple[str, int]) -> None:
        self.listener = socket.create_server(address, backlog=64)
        self.address = self.listener.getsockname()[:2]
        threading.Thread(target=self._accept, daemon=True).start()

    def open(self, host: str, port: int) -> socket.socket:
        """A connection to host:port."""
        raise NotImplementedError

    def close(self) -> None:
        """Stops listening."""
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            head, rest = read_head(client)
            method, host, port, upstream_head = target_of(head)
            upstream = self.open(host, port)
        except (EgressError, OSError, ValueError) as error:
            with contextlib.suppress(OSError):
                message = f"{self.name}: {error}\n".encode()
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
                    + f"Content-Length: {len(message)}\r\nConnection: close\r\n\r\n".encode()
                    + message
                )
            client.close()
            return
        if method == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            upstream.sendall(upstream_head)
        if rest:
            upstream.sendall(rest)
        splice(client, upstream)


class EgressProxy(HttpProxy):
    """The node's proxy on NODE_IP:PROXY_PORT, pods' way out. `resolve` maps a cluster name
    to its ClusterIP (None: not a cluster name); `relay` is the supervisor's Unix socket."""

    name = "gluon-kube egress"

    def __init__(self, resolve: Callable[[str], str | None], relay: Path) -> None:
        self.resolve = resolve
        self.relay = relay
        super().__init__((NODE_IP, PROXY_PORT))

    def open(self, host: str, port: int) -> socket.socket:
        """A connection to host:port: directly for the cluster, else through the relay."""
        cluster_ip = self.resolve(host)
        if cluster_ip is not None or in_cluster(host):
            upstream = socket.create_connection((cluster_ip or host, port), CONNECT_TIMEOUT_S)
            upstream.settimeout(None)
            return upstream
        relay = socket.socket(socket.AF_UNIX)
        relay.settimeout(CONNECT_TIMEOUT_S * 2)
        try:
            relay.connect(str(self.relay))
            relay.sendall(f"{host} {port}\n".encode())
            answer = b""
            while not answer.endswith(b"\n"):
                chunk = relay.recv(1)
                if not chunk:
                    break
                answer += chunk
        except OSError as error:
            relay.close()
            raise EgressError(f"the relay: {error}") from None
        if answer != b"ok\n":
            relay.close()
            raise EgressError(f"{host}:{port}: {answer.decode(errors='replace').strip()}")
        relay.settimeout(None)
        return relay
