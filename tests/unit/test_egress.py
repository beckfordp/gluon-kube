# SPDX-License-Identifier: MIT

"""The egress proxy's request handling (GK-5.3): CONNECT and absolute-form HTTP requests,
as an HTTP proxy receives them (RFC 9110 §9.3.6, RFC 9112 §3.2.2), and which hosts are
the cluster's."""

import pytest

from gluon_kube.egress import EgressError, in_cluster, target_of


def test_connect_gives_the_authority() -> None:
    head = b"CONNECT repo1.maven.org:443 HTTP/1.1\r\nHost: repo1.maven.org:443\r\n\r\n"
    assert target_of(head) == ("CONNECT", "repo1.maven.org", 443, b"")


def test_absolute_form_is_rewritten_to_origin_form() -> None:
    head = (
        b"GET http://kafka-connect:8083/connectors?expand=status HTTP/1.1\r\n"
        b"Host: kafka-connect:8083\r\n\r\n"
    )
    method, host, port, upstream = target_of(head)
    assert (method, host, port) == ("GET", "kafka-connect", 8083)
    assert upstream == (
        b"GET /connectors?expand=status HTTP/1.1\r\nHost: kafka-connect:8083\r\n"
        b"Connection: close\r\n\r\n"
    )


def test_one_request_per_connection() -> None:
    """A client's keep-alive is dropped: its next request may be for another host."""
    head = (
        b"GET http://user-service:8080/docs/ HTTP/1.1\r\nHost: user-service:8080\r\n"
        b"Proxy-Connection: keep-alive\r\nConnection: keep-alive\r\nKeep-Alive: 300\r\n"
        b"Accept: */*\r\n\r\n"
    )
    upstream = target_of(head)[3]
    assert upstream == (
        b"GET /docs/ HTTP/1.1\r\nHost: user-service:8080\r\nAccept: */*\r\n"
        b"Connection: close\r\n\r\n"
    )
    assert target_of(b"PUT http://x/ HTTP/1.1\r\n\r\n")[2] == 80


@pytest.mark.parametrize(
    "head",
    [b"nonsense\r\n\r\n", b"GET https://x/ HTTP/1.1\r\n\r\n", b"GET /relative HTTP/1.1\r\n\r\n"],
)
def test_other_requests_are_refused(head: bytes) -> None:
    with pytest.raises(EgressError):
        target_of(head)


@pytest.mark.parametrize(
    ("host", "inside"),
    [("10.244.0.7", True), ("10.96.3.4", True), ("8.8.8.8", False), ("kafka", False)],
)
def test_cluster_addresses(host: str, inside: bool) -> None:
    assert in_cluster(host) is inside
