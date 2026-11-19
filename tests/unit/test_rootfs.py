# SPDX-License-Identifier: MIT

"""A container's root as bwrap arguments (GK-4.4): the host read-only, the container's
own /dev, /proc and /tmp, and mounts whose targets the host lacks made possible by
overlaying the directory that lacks them (GK-1.6's finding)."""

from pathlib import Path

import pytest

from gluon_kube.rootfs import Bind, Mount, Symlink, Tmpfs, overlays, root_args


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """A small host tree: /etc/{hosts,passwd}, /usr/bin/sh, /var/lib, /dev, /proc, /tmp,
    /lib -> usr/lib."""
    for directory in ("etc", "usr/bin", "usr/lib", "var/lib", "dev", "proc", "tmp"):
        (tmp_path / directory).mkdir(parents=True)
    for file in ("etc/hosts", "etc/passwd", "usr/bin/sh"):
        (tmp_path / file).write_text("x")
    (tmp_path / "lib").symlink_to("usr/lib")
    return tmp_path


def pairs(args: list[str]) -> list[tuple[str, ...]]:
    """bwrap args grouped by option."""
    result: list[tuple[str, ...]] = []
    i = 0
    while i < len(args):
        width = 2 if args[i] in ("--tmpfs", "--proc") else 3
        result.append(tuple(args[i : i + width]))
        i += width
    return result


def test_no_mounts_is_the_host_read_only_with_own_dev_proc_tmp(host: Path) -> None:
    assert pairs(root_args([], host)) == [
        ("--dev-bind", "/dev", "/dev"),
        ("--ro-bind", str(host / "etc"), "/etc"),
        ("--symlink", "usr/lib", "/lib"),
        ("--proc", "/proc"),
        ("--tmpfs", "/tmp"),
        ("--ro-bind", str(host / "usr"), "/usr"),
        ("--ro-bind", str(host / "var"), "/var"),
    ]


def test_a_target_over_an_existing_file_needs_no_overlay(host: Path) -> None:
    hosts = Bind(Path("/state/hosts"), "/etc/hosts")
    assert overlays([hosts], host) == ["/"]
    assert pairs(root_args([hosts], host))[-1] == ("--ro-bind", "/state/hosts", "/etc/hosts")


def test_a_new_top_level_target_goes_in_the_root_tmpfs(host: Path) -> None:
    assert overlays([Bind(Path("/cm"), "/connectors")], host) == ["/"]


def test_a_target_the_host_directory_lacks_overlays_that_directory(host: Path) -> None:
    mount = Bind(Path("/pkg/kafka-docker"), "/etc/kafka/docker")
    assert overlays([mount], host) == ["/", "/etc"]
    args = pairs(root_args([mount], host))
    assert ("--ro-bind", str(host / "etc"), "/etc") not in args
    tmpfs = args.index(("--tmpfs", "/etc"))
    assert args[tmpfs + 1 : tmpfs + 3] == [
        ("--ro-bind", str(host / "etc" / "hosts"), "/etc/hosts"),
        ("--ro-bind", str(host / "etc" / "passwd"), "/etc/passwd"),
    ]
    assert args[-1] == ("--ro-bind", "/pkg/kafka-docker", "/etc/kafka/docker")


def test_a_deep_target_overlays_only_the_directory_that_lacks_it(host: Path) -> None:
    mount = Symlink("/usr/bin/docker-entrypoint.sh", "/x")
    assert overlays([mount], host) == ["/", "/usr/bin"]
    args = pairs(root_args([mount], host))
    assert args.index(("--ro-bind", str(host / "usr"), "/usr")) < args.index(
        ("--tmpfs", "/usr/bin")
    )


def test_targets_inside_our_own_mounts_need_no_overlay(host: Path) -> None:
    mounts: list[Mount] = [
        Bind(Path("/state/data"), "/var/lib/postgresql/data", writable=True),
        Tmpfs("/var/lib/postgresql"),
    ]
    assert overlays(mounts, host) == ["/", "/var/lib"]
    args = pairs(root_args(mounts, host))
    assert args[-2:] == [
        ("--tmpfs", "/var/lib/postgresql"),
        ("--bind", "/state/data", "/var/lib/postgresql/data"),
    ]


def test_through_a_host_symlink_nothing_is_overlaid(host: Path) -> None:
    assert overlays([Tmpfs("/lib/x")], host) == ["/"]


def test_unreadable_host_entries_are_left_out(host: Path) -> None:
    secret = host / "secret"
    secret.mkdir(mode=0)
    try:
        assert "/secret" not in root_args([], host)
    finally:
        secret.chmod(0o700)
