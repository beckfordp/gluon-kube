# SPDX-License-Identifier: MIT

"""Fakes for the controller's Runtime: record what is asked, answer probes from a table,
and a clock the test moves."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gluon_kube.controller import ContainerRecord, PodRecord
from gluon_kube.model import Probe


@dataclass
class FakeProcess:
    pid: int
    code: int | None = None
    terminated: bool = False
    killed: bool = False
    graceful: bool = False
    """Exits 0 on SIGTERM, as a server does."""

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        if self.graceful:
            self.code = 0

    def kill(self) -> None:
        self.killed = True
        if self.code is None:
            self.code = 137


@dataclass
class FakeSandbox:
    ip: str
    pid: int


@dataclass
class FakeRuntime:
    """Records what the controller asks for; probes answer from `answers`."""

    started: list[tuple[str, str]] = field(default_factory=list)
    processes: dict[tuple[str, str], FakeProcess] = field(default_factory=dict)
    sandboxes: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    services: dict[str, tuple[str, list[tuple[int, list[tuple[str, int]]]]]] = field(
        default_factory=dict
    )
    removed: list[str] = field(default_factory=list)
    files: dict[Path, str] = field(default_factory=dict)
    answers: dict[str, bool] = field(default_factory=dict)
    pids: int = 100
    graceful: bool = False

    def start_sandbox(self, pod: PodRecord) -> FakeSandbox:
        self.sandboxes.append(pod.name)
        return FakeSandbox(f"10.244.0.{len(self.sandboxes) + 1}", 1)

    def stop_sandbox(self, pod: PodRecord) -> None:
        self.stopped.append(pod.name)

    def start_container(self, pod: PodRecord, container: ContainerRecord) -> FakeProcess:
        self.pids += 1
        process = FakeProcess(self.pids, graceful=self.graceful)
        self.started.append((pod.name, container.name))
        self.processes[(pod.name, container.name)] = process
        return process

    def probe(self, pod: PodRecord, container: ContainerRecord, probe: Probe) -> bool:
        return self.answers.get(container.name, True)

    def set_service(
        self, name: str, ip: str, ports: Sequence[tuple[int, Sequence[tuple[str, int]]]]
    ) -> None:
        self.services[name] = (ip, [(p, list(e)) for p, e in ports])

    def remove_service(self, name: str, ip: str) -> None:
        self.removed.append(name)
        self.services.pop(name, None)

    def write_file(self, path: Path, text: str) -> None:
        self.files[path] = text


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now
