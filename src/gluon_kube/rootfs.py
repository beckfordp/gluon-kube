# SPDX-License-Identifier: MIT

"""A container's root filesystem as bwrap arguments (ADR 0030 decision 3; GK-1.6, GK-4.4).

The root is the host's, read-only, with the container's own mounts on top: the image's
files (a recipe's), its volumes (emptyDir, ConfigMap) and what Kubernetes gives every
container (/etc/hosts). bwrap can't create a mount point inside a directory bound from
the host (GK-1.6: `/connectors`, `/etc/kafka`), so a host directory that lacks a mount
target is *overlaid*: a tmpfs in its place with the host's entries bound back into it.
`/` is always overlaid; `/dev`, `/proc` and `/tmp` are the container's own, as in a
container.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class Bind:
    """A host file or directory at `target`; read-only unless `writable`."""

    source: Path
    target: str
    writable: bool = False

    def args(self) -> list[str]:
        """Its bwrap arguments."""
        return ["--bind" if self.writable else "--ro-bind", str(self.source), self.target]


@dataclass(frozen=True, slots=True)
class Tmpfs:
    """An empty, writable directory at `target` (gone with the container)."""

    target: str

    def args(self) -> list[str]:
        """Its bwrap arguments."""
        return ["--tmpfs", self.target]


@dataclass(frozen=True, slots=True)
class Symlink:
    """A symlink at `target` pointing to `points_to`."""

    target: str
    points_to: str

    def args(self) -> list[str]:
        """Its bwrap arguments."""
        return ["--symlink", self.points_to, self.target]


type Mount = Bind | Tmpfs | Symlink

OWN = {
    "dev": ["--dev-bind", "/dev", "/dev"],
    "proc": ["--proc", "/proc"],
    "tmp": ["--tmpfs", "/tmp"],
}
"""Top-level directories a container has its own of."""


def depth(target: str) -> int:
    """How many components a path has (parents mount before their children)."""
    return len(PurePosixPath(target).parts)


def inside(path: str, roots: Iterable[str]) -> bool:
    """`path` is one of `roots` or below one."""
    pure = PurePosixPath(path)
    return any(pure == PurePosixPath(root) or PurePosixPath(root) in pure.parents for root in roots)


def on_host(host: Path, path: str) -> Path:
    """Where `path` (absolute, in the container) is on the host."""
    return host / path.lstrip("/")


def overlays(mounts: Sequence[Mount], host: Path) -> list[str]:
    """The host directories to overlay so every mount target can be created."""
    overlaid = ["/"]
    ours: list[str] = []
    for mount in sorted(mounts, key=lambda m: depth(m.target)):
        current = "/"
        for part in PurePosixPath(mount.target).parts[1:]:
            child = str(PurePosixPath(current) / part)
            if inside(current, ours):
                break  # in a mount of our own: bwrap creates the child
            exists = os.path.lexists(on_host(host, child))
            if current in overlaid and not exists:
                break  # a tmpfs: bwrap creates the child
            if not exists:
                overlaid.append(current)
                break
            if on_host(host, child).is_symlink():
                break  # a symlink from the host: mounting through it is the caller's problem
            current = child
        ours.append(mount.target)
    return overlaid


def entry_args(host: Path, directory: str, overlaid: Sequence[str]) -> list[str]:
    """The host's entries of an overlaid `directory`, bound back in (read-only)."""
    args: list[str] = []
    source_dir = on_host(host, directory)
    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name):
        target = str(PurePosixPath(directory) / entry.name)
        if target in overlaid:
            continue  # overlaid itself, after this one
        if directory == "/" and entry.name in OWN:
            args += OWN[entry.name]
        elif entry.is_symlink():
            args += ["--symlink", os.readlink(entry), target]
        elif os.access(entry, os.R_OK | (os.X_OK if entry.is_dir() else 0)):
            args += ["--ro-bind", str(entry), target]
    return args


def root_args(mounts: Sequence[Mount], host: Path = Path("/")) -> list[str]:
    """bwrap arguments for a root: the host's, read-only, then `mounts` (parents first)."""
    overlaid = overlays(mounts, host)
    args: list[str] = []
    for directory in sorted(overlaid, key=depth):
        if directory != "/":
            args += ["--tmpfs", directory]
        args += entry_args(host, directory, overlaid)
    for mount in sorted(mounts, key=lambda m: depth(m.target)):
        args += mount.args()
    return args
