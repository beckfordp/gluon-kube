# SPDX-License-Identifier: MIT

"""Reading a Rust port's Dockerfile (GK-6.2): its stages and the image's config, which
must be what `docker image inspect` showed for the image built from it on the laptop
(laptop-reference/images/user-service-rs_latest.inspect.json)."""

import json
from pathlib import Path

import pytest

from gluon_kube.build import cargo_commands, renders_asyncapi
from gluon_kube.dockerfile import CopyFrom, DockerfileError, parse, read
from gluon_kube.images import ImageConfig

GLUON = Path(__file__).resolve().parents[4]
REFERENCE = Path(__file__).resolve().parents[2] / "laptop-reference" / "images"
USER_SERVICE_RS = GLUON / "services" / "user-service-rs" / "Dockerfile"


@pytest.mark.skipif(not USER_SERVICE_RS.exists(), reason="needs gluon's user-service-rs")
def test_user_service_rs_config_is_the_images() -> None:
    inspect = json.loads((REFERENCE / "user-service-rs_latest.inspect.json").read_text())
    assert read(USER_SERVICE_RS).config() == ImageConfig.from_inspect(inspect[0]["Config"])


@pytest.mark.skipif(not USER_SERVICE_RS.exists(), reason="needs gluon's user-service-rs")
def test_user_service_rs_stages() -> None:
    dockerfile = read(USER_SERVICE_RS)
    assert [s.name for s in dockerfile.stages] == ["build", "docs", None]
    assert dockerfile.stage("build").workdir == "/src/services/user-service-rs"
    assert cargo_commands(dockerfile)[0][:4] == ["cargo", "build", "--release", "--locked"]
    assert renders_asyncapi(dockerfile)
    assert dockerfile.image.copies[0] == CopyFrom(
        "build",
        ("/src/services/user-service-rs/target/release/user-service",),
        "/usr/local/bin/user-service",
    )


def test_instructions() -> None:
    dockerfile = parse(
        """\
# a comment
ARG X=1
FROM rust AS build
WORKDIR /src
WORKDIR app
RUN cargo build \\
    --release
RUN echo not cargo
FROM debian
ENV A=1 B="two words"
ENV C three
EXPOSE 8080 9090
USER 10:20
CMD ["serve", "--port", "1"]
COPY --from=build --chown=1 /src/app/target/x /usr/bin/x
COPY local /etc/local
LABEL a=b
"""
    )
    build, image = dockerfile.stages
    assert build.workdir == "/src/app"
    assert cargo_commands(dockerfile) == [["cargo", "build", "--release"]]
    assert not renders_asyncapi(dockerfile)
    config = dockerfile.config()
    assert dict(config.env) == {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "A": "1",
        "B": "two words",
        "C": "three",
    }
    assert (config.user, config.cmd, config.entrypoint) == ("10:20", ("serve", "--port", "1"), ())
    assert image.exposed == ["8080", "9090"]
    assert image.copies == [CopyFrom("build", ("/src/app/target/x",), "/usr/bin/x")]


def test_shell_form_and_entrypoint_resets_cmd() -> None:
    dockerfile = parse('FROM x\nCMD ["a"]\nENTRYPOINT run it\n')
    assert dockerfile.config().entrypoint == ("/bin/sh", "-c", "run it")
    assert dockerfile.config().cmd == ()


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "no FROM"),
        ("RUN x\n", "RUN before FROM"),
        ("FROM x\nFROBNICATE y\n", "unknown instruction FROBNICATE"),
        ('FROM x\nCMD ["a", 1]\n', "CMD"),
    ],
)
def test_what_cant_be_read_is_refused(text: str, problem: str) -> None:
    with pytest.raises(DockerfileError, match=problem):
        parse(text)


def test_a_missing_dockerfile(tmp_path: Path) -> None:
    with pytest.raises(DockerfileError, match="no such file"):
        read(tmp_path / "Dockerfile")
    with pytest.raises(DockerfileError, match="no stage named nope"):
        parse("FROM x\n").stage("nope")
