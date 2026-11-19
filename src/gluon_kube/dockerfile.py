# SPDX-License-Identifier: MIT

"""A Dockerfile read as far as a Rust port's image recipe needs it (GK-6.2): its stages,
the final stage's config (ENTRYPOINT, CMD, ENV, WORKDIR, USER, EXPOSE) and what it copies
from earlier stages, and the build stage's `cargo` commands. The recipe is read from the
repo, not restated in gluon-kube (ADR 0030 decision 4); anything it can't read is an
error naming the line."""

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from gluon_kube.images import ImageConfig


class DockerfileError(Exception):
    """A Dockerfile gluon-kube can't read; the message names the file and line."""


@dataclass(frozen=True, slots=True)
class CopyFrom:
    """`COPY --from=STAGE SOURCE... DEST` in the final stage."""

    stage: str
    sources: tuple[str, ...]
    dest: str


@dataclass(slots=True)
class Stage:
    """A build stage: its base, name, working directory, RUN commands and copies."""

    base: str
    name: str | None
    workdir: str = "/"
    runs: list[str] = field(default_factory=list)
    copies: list[CopyFrom] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    entrypoint: tuple[str, ...] = ()
    cmd: tuple[str, ...] = ()
    user: str | None = None
    exposed: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Dockerfile:
    """The stages, in order; the last is the image."""

    stages: tuple[Stage, ...]

    @property
    def image(self) -> Stage:
        """The final stage."""
        return self.stages[-1]

    def stage(self, name: str) -> Stage:
        """A stage by name."""
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise DockerfileError(f"no stage named {name}")

    def config(self) -> ImageConfig:
        """The image's config, as `docker image inspect` would show it (PATH as the
        Debian-based images set it, unless the Dockerfile sets one)."""
        image = self.image
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        env.update(image.env)
        return ImageConfig(
            entrypoint=image.entrypoint,
            cmd=image.cmd,
            env=tuple(env.items()),
            user=image.user,
            working_dir=image.workdir,
        )


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(line number, instruction) with continuations joined and comments dropped."""
    result: list[tuple[int, str]] = []
    current, start = "", 0
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if not current:
            start = number
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        result.append((start, current + stripped))
        current = ""
    if current:
        result.append((start, current))
    return result


def command_form(value: str) -> tuple[str, ...]:
    """ENTRYPOINT/CMD: exec form (JSON) as is; shell form as `/bin/sh -c`."""
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
            raise ValueError("exec form must be a JSON list of strings")
        return tuple(parsed)
    return ("/bin/sh", "-c", value)


ENV_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse(text: str, source: str = "Dockerfile") -> Dockerfile:
    """The Dockerfile's stages."""
    stages: list[Stage] = []
    for number, line in logical_lines(text):
        word, _, rest = line.partition(" ")
        instruction, rest = word.upper(), rest.strip()
        if instruction == "FROM":
            parts = rest.split()
            name = parts[2] if len(parts) >= 3 and parts[1].upper() == "AS" else None
            stages.append(Stage(base=parts[0], name=name))
            continue
        if not stages:
            if instruction == "ARG":
                continue  # a build argument for FROM lines
            raise DockerfileError(f"{source}:{number}: {instruction} before FROM")
        stage = stages[-1]
        try:
            match instruction:
                case "WORKDIR":
                    stage.workdir = str(Path(stage.workdir) / rest)
                case "RUN":
                    stage.runs.append(rest)
                case "ENV":
                    if "=" in rest.split()[0]:
                        for key, value in ENV_PAIR.findall(rest):
                            stage.env[key] = shlex.split(value)[0] if value else ""
                    else:
                        key, _, value = rest.partition(" ")
                        stage.env[key] = value.strip()
                case "ENTRYPOINT":
                    stage.entrypoint = command_form(rest)
                    stage.cmd = ()
                case "CMD":
                    stage.cmd = command_form(rest)
                case "USER":
                    stage.user = rest
                case "EXPOSE":
                    stage.exposed += rest.split()
                case "COPY":
                    args = shlex.split(rest)
                    options = [a for a in args if a.startswith("--")]
                    paths = [a for a in args if not a.startswith("--")]
                    origin = next(
                        (o.split("=", 1)[1] for o in options if o.startswith("--from=")), None
                    )
                    if origin is not None and len(paths) >= 2:
                        stage.copies.append(CopyFrom(origin, tuple(paths[:-1]), paths[-1]))
                case (
                    "ARG"
                    | "LABEL"
                    | "SHELL"
                    | "STOPSIGNAL"
                    | "HEALTHCHECK"
                    | "VOLUME"
                    | "ADD"
                    | "ONBUILD"
                ):
                    pass
                case _:
                    raise DockerfileError(f"{source}:{number}: unknown instruction {instruction}")
        except (ValueError, IndexError) as error:
            raise DockerfileError(f"{source}:{number}: {instruction}: {error}") from None
    if not stages:
        raise DockerfileError(f"{source}: no FROM")
    return Dockerfile(tuple(stages))


def read(path: Path) -> Dockerfile:
    """A Dockerfile from disk."""
    try:
        return parse(path.read_text(), str(path))
    except FileNotFoundError:
        raise DockerfileError(f"{path}: no such file") from None
