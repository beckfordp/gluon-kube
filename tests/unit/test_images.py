# SPDX-License-Identifier: MIT

"""The image catalogue (GK-4.7): each recipe's image config is the image's own, as
`docker image inspect` showed it on the laptop (laptop-reference/images/), and its
captured files are copied unchanged; an image with no recipe is unavailable, saying why."""

import json
from pathlib import Path

import pytest

from gluon_kube.images import (
    HOST_IMAGE,
    PACKAGE,
    HostTools,
    ImageConfig,
    Recipe,
    Unavailable,
    account,
    captured,
    recipe_for,
    recipes,
)
from gluon_kube.rootfs import Bind, Tmpfs

REFERENCE = Path(__file__).resolve().parents[2] / "laptop-reference" / "images"


def inspect(name: str) -> ImageConfig:
    return ImageConfig.from_inspect(
        json.loads((REFERENCE / f"{name}.inspect.json").read_text())[0]["Config"]
    )


def tools(tmp_path: Path) -> HostTools:
    pg_bin = tmp_path / "usr" / "lib" / "postgresql" / "16" / "bin"
    pg_bin.mkdir(parents=True)
    share = tmp_path / "usr" / "share" / "postgresql" / "16"
    share.mkdir(parents=True)
    (share / "postgresql.conf.sample").write_text("#listen_addresses = 'localhost'\n")
    return HostTools(pg_bin=pg_bin, uid=1000, gid=1000, home=tmp_path, cache=tmp_path / "cache")


@pytest.mark.parametrize(
    ("recipe", "capture"), [("postgres", "postgres_16-alpine"), ("redis", "redis_7-alpine")]
)
def test_captured_configs_are_the_images(recipe: str, capture: str) -> None:
    assert captured(recipe) == inspect(capture)


@pytest.mark.parametrize(
    ("packaged", "capture"),
    [
        ("postgres/docker-entrypoint.sh", "postgres_16-alpine.docker-entrypoint.sh"),
        ("postgres/passwd", "postgres_16-alpine_etc_passwd"),
        ("postgres/group", "postgres_16-alpine_etc_group"),
        ("redis/docker-entrypoint.sh", "redis_7-alpine.docker-entrypoint.sh"),
        ("redis/passwd", "redis_7-alpine_etc_passwd"),
        ("redis/group", "redis_7-alpine_etc_group"),
    ],
)
def test_packaged_files_are_the_captured_ones(packaged: str, capture: str) -> None:
    assert (PACKAGE / packaged).read_bytes() == (REFERENCE / capture).read_bytes()


def test_postgres_runs_the_images_entrypoint_as_its_user(tmp_path: Path) -> None:
    recipe = recipe_for("postgres:16-alpine", tools(tmp_path))
    assert isinstance(recipe, Recipe)
    assert recipe.config.entrypoint == ("docker-entrypoint.sh",)
    assert recipe.config.cmd == ("postgres",)
    assert (recipe.uid, recipe.gid, recipe.home) == (70, 70, "/var/lib/postgresql")
    env = recipe.image_env()
    assert env["PATH"].startswith(str(tmp_path / "usr" / "lib" / "postgresql" / "16" / "bin") + ":")
    assert env["PGDATA"] == "/var/lib/postgresql/data"
    assert env["LANG"] == "C.UTF-8"
    targets = {m.target: m for m in recipe.mounts}
    entrypoint = targets["/usr/local/bin/docker-entrypoint.sh"]
    assert isinstance(entrypoint, Bind)
    assert entrypoint.source == PACKAGE / "postgres" / "docker-entrypoint.sh"
    assert isinstance(targets["/var/lib/postgresql"], Tmpfs)
    sample = targets[
        str(tmp_path / "usr" / "share" / "postgresql" / "16" / "postgresql.conf.sample")
    ]
    assert isinstance(sample, Bind)
    assert "listen_addresses = '*'" in sample.source.read_text()
    assert recipe.differences


def test_no_postgres_binaries_no_recipe() -> None:
    result = recipe_for("postgres:16-alpine", HostTools())
    assert isinstance(result, Unavailable)
    assert "pg_ctl" in result.reason


def test_unknown_images_are_unavailable() -> None:
    image = "apicurio/apicurio-registry:3.3.2"
    result = recipe_for(image, HostTools())
    assert result == Unavailable(image, f"no gluon-kube recipe for {image}")
    assert "postgres:16-alpine" in recipes()


def test_the_host_image_is_the_sandbox_as_its_user(tmp_path: Path) -> None:
    recipe = recipe_for(HOST_IMAGE, tools(tmp_path))
    assert isinstance(recipe, Recipe)
    assert (recipe.uid, recipe.gid) == (1000, 1000)
    assert any(
        isinstance(m, Bind) and m.writable and m.target == str(tmp_path) for m in recipe.mounts
    )


def test_accounts_by_name_or_uid() -> None:
    passwd = PACKAGE / "postgres" / "passwd"
    assert account(passwd, "postgres") == (70, 70, "/var/lib/postgresql")
    assert account(passwd, "70")[0] == 70
    with pytest.raises(KeyError):
        account(passwd, "nobody-here")


def test_discover_finds_what_the_sandbox_has(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GLUON_KUBE_CDC_HOME", str(tmp_path))
    found = HostTools.discover()
    assert found.cdc_home is None
    (tmp_path / "COMPLETE").touch()
    assert HostTools.discover().cdc_home == tmp_path


def cdc_tools(tmp_path: Path) -> HostTools:
    """A sandbox with the CDC jars, Java and a CA bundle."""
    cdc = tmp_path / "cdc"
    for sub in ("kafka", "connect", "plugins/debezium-connector-postgres"):
        (cdc / sub).mkdir(parents=True)
    bundle = tmp_path / "ca.crt"
    bundle.write_text("pem")
    return HostTools(cdc_home=cdc, java_home=tmp_path / "jdk", ca_bundle=bundle)


@pytest.mark.parametrize(
    ("recipe", "capture"),
    [
        ("kafka", "apache_kafka_3.8.0"),
        ("connect", "debezium_connect_3.0.0.Final"),
        ("curl", "curlimages_curl_8.11.1"),
    ],
)
def test_gk5_captured_configs_are_the_images(recipe: str, capture: str) -> None:
    assert captured(recipe) == inspect(capture)


@pytest.mark.parametrize(
    ("packaged", "capture"),
    [
        ("kafka/passwd", "apache_kafka_3.8.0_etc_passwd"),
        ("connect/docker-entrypoint.sh", "debezium_connect_3.0.0.Final.docker-entrypoint.sh"),
        ("connect/passwd", "debezium_connect_3.0.0.Final_etc_passwd"),
        ("curl/passwd", "curlimages_curl_8.11.1_etc_passwd"),
        ("curl/group", "curlimages_curl_8.10.1_etc_group"),
    ],
)
def test_gk5_packaged_files_are_the_captured_ones(packaged: str, capture: str) -> None:
    assert (PACKAGE / packaged).read_bytes() == (REFERENCE / capture).read_bytes()


def test_kafkas_docker_scripts_are_the_images(tmp_path: Path) -> None:
    import tarfile

    with tarfile.open(REFERENCE / "apache_kafka_3.8.0.etc-kafka-docker.tar") as archive:
        for member in archive.getmembers():
            if member.isfile():
                data = archive.extractfile(member)
                assert data is not None
                assert (
                    PACKAGE / "kafka" / "etc-kafka-docker" / member.name
                ).read_bytes() == data.read()


def test_kafka_recipe(tmp_path: Path) -> None:
    tools = cdc_tools(tmp_path)
    recipe = recipe_for("apache/kafka:3.8.0", tools)
    assert isinstance(recipe, Recipe)
    assert (recipe.uid, recipe.gid) == (1000, 1000)
    assert recipe.config.cmd == ("/etc/kafka/docker/run",)
    targets = {m.target: m for m in recipe.mounts}
    libs = targets["/opt/kafka/libs"]
    assert isinstance(libs, Bind)
    assert libs.source == tools.cdc_home / "kafka"  # type: ignore[operator]
    assert "/opt/java/openjdk" in targets
    assert [c.target for c in recipe.copies] == ["/opt/kafka/config"]
    assert not recipe.egress


def test_connect_and_curl_recipes(tmp_path: Path) -> None:
    tools = cdc_tools(tmp_path)
    connect = recipe_for("debezium/connect:3.0.0.Final", tools)
    assert isinstance(connect, Recipe)
    assert (connect.uid, connect.config.working_dir) == (1001, "/kafka")
    targets = {m.target for m in connect.mounts}
    assert {"/docker-entrypoint.sh", "/usr/local/sbin/ip", "/etc/redhat-release"} <= targets
    assert "/kafka/connect/debezium-connector-postgres" in targets
    for image in ("curlimages/curl:8.10.1", "curlimages/curl:8.11.1"):
        curl = recipe_for(image, tools)
        assert isinstance(curl, Recipe)
        assert curl.egress
        assert curl.image_env()["CURL_CA_BUNDLE"] == "/cacert.pem"


@pytest.mark.parametrize(
    "image", ["apache/kafka:3.8.0", "debezium/connect:3.0.0.Final", "curlimages/curl:8.11.1"]
)
def test_missing_sandbox_parts_make_images_unavailable(image: str) -> None:
    assert isinstance(recipe_for(image, HostTools()), Unavailable)


def test_kafka_without_java_is_unavailable(tmp_path: Path) -> None:
    tools = cdc_tools(tmp_path)
    no_java = HostTools(cdc_home=tools.cdc_home)
    result = recipe_for("apache/kafka:3.8.0", no_java)
    assert isinstance(result, Unavailable)
    assert "Java" in result.reason


def rust_port(tmp_path: Path, built: bool) -> HostTools:
    """A gluon tree with a Rust port whose Dockerfile copies a binary and rendered HTML."""
    service = tmp_path / "gluon" / "services" / "demo-rs"
    service.mkdir(parents=True)
    (service / "Dockerfile").write_text(
        "FROM rust AS build\nWORKDIR /src/services/demo-rs\nRUN cargo build --release\n"
        "FROM node AS docs\nRUN asyncapi generate fromTemplate x --output /docs/html\n"
        "FROM debian:bookworm-slim\n"
        "COPY --from=build /src/services/demo-rs/target/release/demo /usr/local/bin/demo\n"
        "COPY --from=docs /docs/html /app/docs\nENV DOCS=/app/docs\nWORKDIR /app\n"
        'USER 65532:65532\nENTRYPOINT ["/usr/local/bin/demo"]\n'
    )
    if built:
        (service / "target" / "release").mkdir(parents=True)
        (service / "target" / "release" / "demo").write_text("binary")
        (service / "target" / "asyncapi-docs").mkdir()
    return HostTools(gluon=tmp_path / "gluon")


def test_a_rust_ports_image_is_read_from_its_dockerfile(tmp_path: Path) -> None:
    tools = rust_port(tmp_path, built=True)
    recipe = recipe_for("demo-rs:latest", tools)
    assert isinstance(recipe, Recipe)
    assert recipe.config.entrypoint == ("/usr/local/bin/demo",)
    assert (recipe.uid, recipe.gid, recipe.home) == (65532, 65532, "/")
    assert recipe.image_env()["DOCS"] == "/app/docs"
    sources = {m.target: m.source for m in recipe.mounts if isinstance(m, Bind)}
    service = tmp_path / "gluon" / "services" / "demo-rs"
    assert sources["/usr/local/bin/demo"] == service / "target" / "release" / "demo"
    assert sources["/app/docs"] == service / "target" / "asyncapi-docs"
    assert sources["/etc/passwd"] == PACKAGE / "debian" / "passwd"


def test_an_unbuilt_rust_port_is_unavailable_saying_how_to_build(tmp_path: Path) -> None:
    tools = rust_port(tmp_path, built=False)
    result = recipe_for("demo-rs:latest", tools)
    assert isinstance(result, Unavailable)
    assert "gluon-kube build demo-rs" in result.reason
    assert isinstance(recipe_for("missing-rs:latest", tools), Unavailable)
    assert isinstance(recipe_for("demo-rs:latest", HostTools()), Unavailable)


def test_redis_runs_the_images_entrypoint_on_the_sandboxs_server(tmp_path: Path) -> None:
    redis_bin = tmp_path / "bin"
    tools = HostTools(redis_bin=redis_bin)
    recipe = recipe_for("redis:7-alpine", tools)
    assert isinstance(recipe, Recipe)
    assert (recipe.uid, recipe.gid) == (999, 1000)
    assert recipe.config.entrypoint == ("docker-entrypoint.sh",)
    assert recipe.config.working_dir == "/data"
    assert recipe.config.cmd == ("redis-server", "--protected-mode", "no")
    sources = {m.target: m.source for m in recipe.mounts if isinstance(m, Bind)}
    assert sources["/usr/local/bin/docker-entrypoint.sh"] == PACKAGE / "redis" / (
        "docker-entrypoint.sh"
    )
    assert sources["/etc/passwd"] == PACKAGE / "redis" / "passwd"
    assert "/data" in {m.target for m in recipe.mounts if isinstance(m, Tmpfs)}
    assert recipe.image_env()["PATH"].startswith(f"{redis_bin}:")
    assert any("protected-mode" in d for d in recipe.differences)


def test_no_redis_server_no_redis_recipe() -> None:
    result = recipe_for("redis:7-alpine", HostTools())
    assert isinstance(result, Unavailable)
    assert "redis-server" in result.reason


def test_a_rust_ports_workdir_exists_when_nothing_is_copied_into_it(tmp_path: Path) -> None:
    service = tmp_path / "gluon" / "services" / "bare-rs"
    (service / "target" / "release").mkdir(parents=True)
    (service / "target" / "release" / "bare").write_text("binary")
    (service / "Dockerfile").write_text(
        "FROM rust AS build\nRUN cargo build --release\nFROM debian:bookworm-slim\n"
        "COPY --from=build /src/services/bare-rs/target/release/bare /usr/local/bin/bare\n"
        'WORKDIR /app\nUSER 65532:65532\nENTRYPOINT ["/usr/local/bin/bare"]\n'
    )
    recipe = recipe_for("bare-rs:latest", HostTools(gluon=tmp_path / "gluon"))
    assert isinstance(recipe, Recipe)
    assert "/app" in {m.target for m in recipe.mounts if isinstance(m, Tmpfs)}


def test_a_rust_ports_workdir_is_mounted_once_beside_its_copies(tmp_path: Path) -> None:
    recipe = recipe_for("demo-rs:latest", rust_port(tmp_path, built=True))
    assert isinstance(recipe, Recipe)
    assert [m.target for m in recipe.mounts].count("/app") == 1
