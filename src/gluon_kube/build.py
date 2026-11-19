# SPDX-License-Identifier: MIT

"""`gluon-kube build SERVICE`: a Rust port's image, as its Dockerfile builds it (GK-6.2),
with the sandbox's toolchain instead of Docker: the build stage's `cargo` commands in
`services/SERVICE` (its WORKDIR is the repo, as `COPY . .` makes it), and, for a
stage that renders the AsyncAPI HTML, the repo's `scripts/asyncapi-html.sh`, which does
what that stage does. What the final stage copies from them is where the recipe
(`images.rust_service`) finds it."""

import shlex
import subprocess
import sys
from pathlib import Path

from gluon_kube.dockerfile import Dockerfile, DockerfileError, read

ASYNCAPI_HTML = Path("scripts") / "asyncapi-html.sh"
"""The repo's script for what a Dockerfile's `docs` stage does (RUST-4.2)."""


def cargo_commands(dockerfile: Dockerfile) -> list[list[str]]:
    """The `cargo` RUN commands of the stages before the image."""
    commands = []
    for stage in dockerfile.stages[:-1]:
        for run in stage.runs:
            argv = shlex.split(run)
            if argv and argv[0] == "cargo":
                commands.append(argv)
    return commands


def renders_asyncapi(dockerfile: Dockerfile) -> bool:
    """Whether a stage renders the AsyncAPI HTML (runs the asyncapi CLI)."""
    return any("asyncapi generate" in run for stage in dockerfile.stages for run in stage.runs)


def build(service_dir: Path) -> int:
    """Builds the port's image parts; the first failing command's exit code, else 0."""
    try:
        dockerfile = read(service_dir / "Dockerfile")
    except DockerfileError as error:
        print(f"gluon-kube: error: {error}", file=sys.stderr)
        return 2
    steps = cargo_commands(dockerfile)
    if renders_asyncapi(dockerfile):
        steps.append(["bash", str(ASYNCAPI_HTML)])
    for argv in steps:
        print(f"== {service_dir.name}: {' '.join(argv)}", flush=True)
        code = subprocess.call(argv, cwd=service_dir)
        if code != 0:
            return code
    return 0
