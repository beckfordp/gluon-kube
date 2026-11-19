# SPDX-License-Identifier: MIT

"""Service ClusterIPs served in the node (kube-proxy's original userspace mode; ADR 0030
decision 3, GK-1.4): each connection to ClusterIP:port is forwarded to one of the
Service's Ready endpoints, round robin; with none the proxy stops listening, so callers
are refused at once, as with a Service that has no Ready pods."""

import contextlib
import itertools
import socket
import threading
from collections.abc import Sequence

CONNECT_TIMEOUT_S = 5.0


def pipe(source: socket.socket, sink: socket.socket) -> None:
    """Copies `source` to `sink` until `source` ends, then ends `sink`'s sending side."""
    with contextlib.suppress(OSError):
        while data := source.recv(65536):
            sink.sendall(data)
    with contextlib.suppress(OSError):
        sink.shutdown(socket.SHUT_WR)


def splice(client: socket.socket, upstream: socket.socket) -> None:
    """Both directions piped, each in a thread; the sockets close when both are done."""

    def both() -> None:
        there = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        there.start()
        pipe(upstream, client)
        there.join()
        client.close()
        upstream.close()

    threading.Thread(target=both, daemon=True).start()


class PortProxy:
    """One Service port: ClusterIP:port to endpoints."""

    def __init__(self, ip: str, port: int) -> None:
        self.address = (ip, port)
        self.endpoints: list[tuple[str, int]] = []
        self.listener: socket.socket | None = None
        self.counter = itertools.count()
        self.lock = threading.Lock()

    def set_endpoints(self, endpoints: Sequence[tuple[str, int]]) -> None:
        """Forward to `endpoints` from now on; none means refuse."""
        with self.lock:
            self.endpoints = list(endpoints)
            if self.endpoints and self.listener is None:
                self.listener = socket.create_server(self.address, backlog=128)
                threading.Thread(target=self._accept, args=(self.listener,), daemon=True).start()
            elif not self.endpoints and self.listener is not None:
                self._close()

    def _close(self) -> None:
        # shutdown, not just close: it stops the listen and wakes the blocked accept()
        # (close alone leaves the kernel socket listening until then; GK-1.4).
        assert self.listener is not None
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.listener = None

    def close(self) -> None:
        """Stops serving."""
        with self.lock:
            self.endpoints = []
            if self.listener is not None:
                self._close()

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            endpoints = self.endpoints
            if not endpoints:
                client.close()
                continue
            endpoint = endpoints[next(self.counter) % len(endpoints)]
            threading.Thread(target=self._forward, args=(client, endpoint), daemon=True).start()

    def _forward(self, client: socket.socket, endpoint: tuple[str, int]) -> None:
        try:
            upstream = socket.create_connection(endpoint, timeout=CONNECT_TIMEOUT_S)
        except OSError:
            client.close()
            return
        upstream.settimeout(None)
        splice(client, upstream)
