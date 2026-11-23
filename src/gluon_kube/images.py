# SPDX-License-Identifier: MIT

"""The image catalogue (ADR 0030 decision 4; GK-4.7, GK-5, GK-6): what runs for an image.

No image can be pulled or run in the sandbox, so each image gluon-local uses maps to a
*recipe*: the image's own config (entrypoint, cmd, env, user, workdir, as `docker image
inspect` shows it on the laptop, `laptop-reference/images/`), its entrypoint scripts and
account files where they are captured, and the sandbox's own binaries where the image
has its own (PostgreSQL, Java, the Kafka jars). Each recipe says what in it is not the
image's (`differences`). An image with no recipe is `ImageUnavailable`.

Package data: `images/<recipe>/` holds the captured files (`config.json`, the image's
`Config` from `docker image inspect`; entrypoints; `passwd`/`group`), copied unchanged
from `laptop-reference/images/` (GK-4.1: run the image's own entrypoint, not a
reimplementation).
"""

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from gluon_kube.rootfs import Bind, Mount, Tmpfs

if TYPE_CHECKING:
    from gluon_kube.dockerfile import Stage

PACKAGE = Path(__file__).resolve().parent / "images"
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CDC_DIR_NAME = "cdc-kafka-3.8.0_debezium-3.0.0.Final"
"""Where gluon bin/sandbox-setup's `cdc` step puts the Kafka, Connect and Debezium jars."""


@dataclass(frozen=True, slots=True)
class ImageConfig:
    """An image's config, as `docker image inspect`'s `.Config`."""

    entrypoint: tuple[str, ...] = ()
    cmd: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    user: str | None = None
    working_dir: str = "/"

    @classmethod
    def from_inspect(cls, config: Mapping[str, object]) -> "ImageConfig":
        """From the `Config` object of `docker image inspect`."""

        def strings(key: str) -> tuple[str, ...]:
            value = config.get(key)
            return tuple(str(item) for item in value) if isinstance(value, list) else ()

        env = tuple(tuple(item.split("=", 1)) for item in strings("Env"))
        user = config.get("User")
        working_dir = config.get("WorkingDir")
        return cls(
            entrypoint=strings("Entrypoint"),
            cmd=strings("Cmd"),
            env=tuple((key, value) for key, value in env),
            user=str(user) if user else None,
            working_dir=str(working_dir) if working_dir else "/",
        )


@dataclass(frozen=True, slots=True)
class Copy:
    """A file or directory of the image that the container may write to: copied fresh
    from `source` at each container start (a container's writable layer)."""

    source: Path
    target: str


@dataclass(frozen=True, slots=True)
class Recipe:
    """How a container of `image` runs here."""

    image: str
    config: ImageConfig
    uid: int
    gid: int
    mounts: tuple[Mount, ...] = ()
    copies: tuple[Copy, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    """Set over the image's env (a difference, listed in `differences`)."""
    path_prepend: tuple[str, ...] = ()
    """Directories put before the image's PATH: where the sandbox has the image's binaries."""
    home: str = "/"
    egress: bool = False
    """The image fetches from the internet: its containers get the node's egress proxy
    (HTTPS_PROXY/HTTP_PROXY; egress.py), as pods here have no route out."""
    differences: tuple[str, ...] = ()

    def image_env(self) -> dict[str, str]:
        """The image's env with the recipe's changes: PATH extended, overrides set."""
        result = dict(self.config.env)
        if self.path_prepend:
            path = result.get("PATH", DEFAULT_PATH)
            result["PATH"] = ":".join((*self.path_prepend, path))
        result.update(self.env)
        return result


@dataclass(frozen=True, slots=True)
class Unavailable:
    """No recipe for an image (or its recipe can't run here): why."""

    image: str
    reason: str


@dataclass(frozen=True, slots=True)
class HostTools:
    """Where the sandbox has what recipes use (found once, at node start)."""

    pg_bin: Path | None = None
    redis_bin: Path | None = None
    """Where `redis-server` is on the PATH (gluon bin/sandbox-setup's `redis` step: the
    image's, or its wrappers over unpacked .debs)."""
    java_home: Path | None = None
    cdc_home: Path | None = None
    ca_bundle: Path | None = None
    uid: int = 0
    gid: int = 0
    home: Path = Path("/")
    path: str = DEFAULT_PATH
    state: Path | None = None
    """The cluster's state directory (its node socket), for `run` pods."""
    gluon: Path | None = None
    """The gluon checkout (services/<port>-rs, for the Rust ports' images)."""
    cache: Path = Path("/tmp/gluon-kube-images")
    """Where recipes write files they derive from the sandbox's (the node's state)."""
    extra: Mapping[str, Path] = field(default_factory=dict)

    @classmethod
    def discover(cls) -> "HostTools":
        """From the sandbox: PostgreSQL's bin directory (pg_ctl's), Java's home (java's),
        the CDC jars (~/.local/<CDC_DIR_NAME>, or GLUON_KUBE_CDC_HOME)."""
        pg_ctl = shutil.which("pg_ctl")
        redis_server = shutil.which("redis-server")
        java = shutil.which("java")
        cdc = Path(os.environ.get("GLUON_KUBE_CDC_HOME", Path.home() / ".local" / CDC_DIR_NAME))
        bundle = Path("/etc/ssl/certs/ca-certificates.crt")
        return cls(
            gluon=gluon_root(),
            ca_bundle=bundle if bundle.is_file() else None,
            pg_bin=Path(pg_ctl).resolve().parent if pg_ctl else None,
            redis_bin=Path(redis_server).parent if redis_server else None,
            java_home=Path(java).resolve().parents[1] if java else None,
            cdc_home=cdc if (cdc / "COMPLETE").exists() else None,
            uid=os.getuid(),
            gid=os.getgid(),
            home=Path.home(),
            path=os.environ.get("PATH", DEFAULT_PATH),
        )


def gluon_root() -> Path | None:
    """The gluon checkout: $GLUON_KUBE_GLUON, else the one this package sits in
    (tools/gluon-kube/src/gluon_kube)."""
    explicit = os.environ.get("GLUON_KUBE_GLUON")
    root = Path(explicit) if explicit else Path(__file__).resolve().parents[4]
    return root if (root / "infra" / "k8s").is_dir() else None


def captured(recipe: str) -> ImageConfig:
    """A recipe's captured image config (package data)."""
    data = json.loads((PACKAGE / recipe / "config.json").read_text())
    return ImageConfig.from_inspect(data)


def account(passwd: Path, name: str) -> tuple[int, int, str]:
    """uid, gid and home of `name` (or of a numeric uid) in a passwd file."""
    for line in passwd.read_text().splitlines():
        fields = line.split(":")
        if len(fields) >= 6 and name in (fields[0], fields[2]):
            return int(fields[2]), int(fields[3]), fields[5]
    raise KeyError(f"{name} not in {passwd}")


def postgresql_sample(pg_bin: Path, cache: Path) -> tuple[Path, Path]:
    """The sandbox's postgresql.conf.sample as the image has it (its Dockerfile:
    `listen_addresses = '*'`), with the socket directory Alpine's build compiles in
    (/var/run/postgresql; Ubuntu's server defaults to /tmp while its libpq looks in
    /var/run/postgresql). Returns (where the sandbox has it, the patched copy)."""
    share = pg_bin.parent.parent.parent.parent / "share" / "postgresql" / pg_bin.parent.name
    original = share / "postgresql.conf.sample"
    patched = cache / "postgres" / "postgresql.conf.sample"
    if not patched.exists():
        text = original.read_text() if original.exists() else ""
        patched.parent.mkdir(parents=True, exist_ok=True)
        patched.write_text(
            text
            + "\n# gluon-kube: as postgres:16-alpine's (its Dockerfile, and Alpine's build)\n"
            + "listen_addresses = '*'\n"
            + "unix_socket_directories = '/var/run/postgresql'\n"
        )
    return original, patched


def postgres(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`postgres:16-alpine`: the image's own docker-entrypoint.sh, run by the sandbox's
    PostgreSQL 16 binaries, as the image's postgres user."""
    if tools.pg_bin is None:
        return Unavailable(image, "no PostgreSQL binaries in the sandbox (pg_ctl not on PATH)")
    files = PACKAGE / "postgres"
    uid, gid, home = account(files / "passwd", "postgres")
    sample, patched = postgresql_sample(tools.pg_bin, tools.cache)
    return Recipe(
        image=image,
        config=captured("postgres"),
        uid=uid,
        gid=gid,
        home=home,
        mounts=(
            Bind(files / "docker-entrypoint.sh", "/usr/local/bin/docker-entrypoint.sh"),
            Bind(files / "passwd", "/etc/passwd"),
            Bind(files / "group", "/etc/group"),
            Bind(patched, str(sample)),
            Tmpfs("/var/lib/postgresql"),
            Tmpfs("/run/postgresql"),
            Tmpfs("/docker-entrypoint-initdb.d"),
        ),
        path_prepend=(str(tools.pg_bin),),
        env=(("LANG", "C.UTF-8"),),
        differences=(
            f"PostgreSQL {tools.pg_bin} (the sandbox's Ubuntu build, 16.x), not the image's",
            "starts as postgres (uid 70), not as root dropping to postgres with gosu: one "
            "uid per user namespace (so `exec` runs as postgres too)",
            "LANG=C.UTF-8: the sandbox has no en_US.utf8 locale",
            "postgresql.conf.sample is the sandbox's, patched as the image's (listen on all "
            "addresses; sockets in /var/run/postgresql)",
            "the sandbox's bash, coreutils and findutils, not Alpine's",
        ),
    )


def redis(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`redis:7-alpine` (cart, catalog and order's Redis; gluon all-Rust Stage B, B4): the
    image's own docker-entrypoint.sh, run by the sandbox's `redis-server`, as the image's
    redis user, in /data."""
    if tools.redis_bin is None:
        return Unavailable(image, "no redis-server in the sandbox (gluon bin/sandbox-setup)")
    files = PACKAGE / "redis"
    uid, gid, home = account(files / "passwd", "redis")
    config = captured("redis")
    return Recipe(
        image=image,
        config=replace(config, cmd=(*config.cmd, "--protected-mode", "no")),
        uid=uid,
        gid=gid,
        home=home,
        mounts=(
            Bind(files / "docker-entrypoint.sh", "/usr/local/bin/docker-entrypoint.sh"),
            Bind(files / "passwd", "/etc/passwd"),
            Bind(files / "group", "/etc/group"),
            Tmpfs("/data"),
        ),
        path_prepend=(str(tools.redis_bin),),
        differences=(
            f"redis-server from {tools.redis_bin} (the sandbox's, Ubuntu's 7.0), not the "
            "image's 7.4",
            "`--protected-mode no` added to the command: the image's build compiles protected "
            "mode off, Ubuntu's leaves it on (connections from other pods would be refused)",
            "starts as redis (uid 999), not as root dropping to redis with setpriv: one uid "
            "per user namespace (so `exec` runs as redis too)",
            "the sandbox's sh and coreutils, not Alpine's",
        ),
    )


HOST_IMAGE = "gluon-kube/host"
"""The image of `gluon-kube run` pods: the sandbox itself, as its user."""


def host(image: str, tools: HostTools) -> Recipe:
    """`gluon-kube run`'s pods: the sandbox's own tools and files, the workspace and home
    writable, as the sandbox user; with the node's socket, so `gluon-kube` works inside
    (as kubectl does in a pod with a service account)."""
    mounts: list[Mount] = [Bind(tools.home, str(tools.home), writable=True)]
    if Path("/workspace").is_dir():
        mounts.insert(0, Bind(Path("/workspace"), "/workspace", writable=True))
    env = [("PATH", tools.path)]
    if tools.state is not None:
        socket = tools.state / "node.sock"
        mounts.append(Bind(socket, str(socket), writable=True))
        env.append(("GLUON_KUBE_STATE", str(tools.state)))
    return Recipe(
        image=image,
        config=ImageConfig(env=tuple(env), working_dir=str(tools.home)),
        uid=tools.uid,
        gid=tools.gid,
        home=str(tools.home),
        mounts=tuple(mounts),
        differences=("not an image: the sandbox's own filesystem",),
    )


def kafka(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`apache/kafka:3.8.0`: the image's own /etc/kafka/docker scripts (run, configure,
    launch: KAFKA_* env to server.properties through KafkaDockerWrapper, storage
    formatted with CLUSTER_ID) and /opt/kafka/config, on the Maven Central jars gluon
    bin/sandbox-setup's `cdc` step installs and the sandbox's Java 21 (GK-5.1)."""
    if tools.cdc_home is None:
        return Unavailable(image, "no Kafka jars (gluon bin/sandbox-setup --only cdc)")
    if tools.java_home is None:
        return Unavailable(image, "no Java in the sandbox")
    files = PACKAGE / "kafka"
    uid, gid, home = account(files / "passwd", "appuser")
    return Recipe(
        image=image,
        config=captured("kafka"),
        uid=uid,
        gid=gid,
        home=home,
        mounts=(
            Bind(PACKAGE / "cacert-entrypoint.sh", "/__cacert_entrypoint.sh"),
            Bind(files / "etc-kafka-docker", "/etc/kafka/docker"),
            Bind(files / "passwd", "/etc/passwd"),
            Bind(files / "group", "/etc/group"),
            Bind(tools.cdc_home / "kafka", "/opt/kafka/libs"),
            Bind(PACKAGE / "kafka-bin", "/opt/kafka/bin"),
            Bind(tools.java_home, "/opt/java/openjdk"),
            Tmpfs("/opt/kafka/logs"),
            Tmpfs(home),
        ),
        copies=(Copy(files / "opt-kafka-config", "/opt/kafka/config"),),
        env=(("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8")),
        differences=(
            "/opt/kafka/libs: the Maven Central jars (kafka_2.13, kafka-tools and their "
            "dependencies), not the binary distribution's libs/",
            "/opt/kafka/bin: gluon-kube's launchers (kafka-run-class.sh, "
            "kafka-server-start.sh and the tools gluon-local calls), not the "
            "distribution's scripts (GK-5.0)",
            "/__cacert_entrypoint.sh: a stand-in that runs its arguments",
            f"Java: the sandbox's ({tools.java_home}), at /opt/java/openjdk",
            "LANG/LC_ALL=C.UTF-8: the sandbox has no en_US.UTF-8 locale",
        ),
    )


def connect(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`debezium/connect:3.0.0.Final`: the image's own /docker-entrypoint.sh (its env to
    connect-distributed.properties) on the Connect runtime and Debezium Postgres plugin
    jars gluon bin/sandbox-setup's `cdc` step installs (GK-5.2)."""
    if tools.cdc_home is None:
        return Unavailable(image, "no Connect jars (gluon bin/sandbox-setup --only cdc)")
    files = PACKAGE / "connect"
    uid, gid, home = account(files / "passwd", "kafka")
    plugin = "debezium-connector-postgres"
    return Recipe(
        image=image,
        config=captured("connect"),
        uid=uid,
        gid=gid,
        home=home,
        mounts=(
            Bind(files / "docker-entrypoint.sh", "/docker-entrypoint.sh"),
            Bind(files / "passwd", "/etc/passwd"),
            Bind(files / "group", "/etc/group"),
            Bind(files / "redhat-release", "/etc/redhat-release"),
            Bind(PACKAGE / "ip", "/usr/local/sbin/ip"),
            Bind(tools.cdc_home / "connect", "/kafka/libs"),
            Bind(tools.cdc_home / "plugins" / plugin, f"/kafka/connect/{plugin}"),
            Bind(PACKAGE / "kafka" / "opt-kafka-config", "/kafka/config.orig"),
            Bind(PACKAGE / "kafka-bin", "/kafka/bin"),
            Tmpfs("/kafka/data"),
            Tmpfs("/kafka/logs"),
            Tmpfs("/kafka/external_libs"),
        ),
        copies=(Copy(files / "kafka-config", "/kafka/config"),),
        differences=(
            "/kafka/libs: the Maven Central Connect runtime (connect-runtime, "
            "connect-json, connect-transforms and their dependencies), not the "
            "image's Kafka distribution",
            f"/kafka/connect: only {plugin} (the image has every Debezium connector)",
            "/kafka/config.orig: Kafka 3.8.0's config/ as apache/kafka ships it",
            "/kafka/bin: gluon-kube's launchers (GK-5.0)",
            "`ip`: a stand-in that prints the pod's address as `ip addr` does",
            "the sandbox's Java 21, bash and coreutils, not Fedora's",
        ),
    )


def curl(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`curlimages/curl` (8.10.1 and 8.11.1: the same config): the sandbox's curl, sh
    and coreutils, as the image's curl_user, with the CA bundle at /cacert.pem
    (CURL_CA_BUNDLE) (GK-5.3)."""
    if tools.ca_bundle is None:
        return Unavailable(image, "no CA bundle in the sandbox")
    files = PACKAGE / "curl"
    uid, gid, home = account(files / "passwd", "curl_user")
    return Recipe(
        image=image,
        config=captured("curl"),
        uid=uid,
        gid=gid,
        home=home,
        mounts=(
            Bind(files / "entrypoint.sh", "/entrypoint.sh"),
            Bind(files / "passwd", "/etc/passwd"),
            Bind(files / "group", "/etc/group"),
            Bind(tools.ca_bundle, "/cacert.pem"),
            Tmpfs(home),
        ),
        egress=True,
        differences=(
            "the sandbox's curl, sh and coreutils, not Alpine's",
            "/cacert.pem: the sandbox's CA bundle",
            "HTTPS_PROXY/HTTP_PROXY: the node's egress proxy (pods have no route out)",
            "/entrypoint.sh: a stand-in, as the image documents it",
        ),
    )


ASYNCAPI_OUTPUT = Path("target") / "asyncapi-docs"
"""Where the repo's scripts/asyncapi-html.sh renders (what a `docs` stage produces)."""


def stage_source(service: Path, gluon: Path, stage: "Stage", path: str) -> Path:
    """Where a build stage's file is here: a stage that renders the AsyncAPI HTML has it in
    ASYNCAPI_OUTPUT; another stage's `/src/<x>` is gluon's `<x>` (the Dockerfile lays the
    repos out as gluon does: WORKDIR /src/services/<port>, gluon-rs at /src/lib)."""
    if any("asyncapi generate" in run for run in stage.runs):
        return service / ASYNCAPI_OUTPUT
    if path.startswith("/src/"):
        return gluon / path.removeprefix("/src/")
    return service / path.lstrip("/")


def rust_service(image: str, tools: HostTools) -> Recipe | Unavailable:
    """`<service>-rs:latest`: a Rust port, as its Dockerfile builds it (GK-6.2): the final
    stage's ENTRYPOINT, CMD, ENV, WORKDIR and USER read from the repo, what it copies from
    the build stages found where `gluon-kube build` (cargo, scripts/asyncapi-html.sh)
    puts them, on the sandbox's root for debian:bookworm-slim's."""
    from gluon_kube.dockerfile import DockerfileError, read

    name = image.split(":", 1)[0]
    if tools.gluon is None:
        return Unavailable(image, "no gluon checkout (GLUON_KUBE_GLUON)")
    service = tools.gluon / "services" / name
    try:
        dockerfile = read(service / "Dockerfile")
    except DockerfileError as error:
        return Unavailable(image, f"{name}'s Dockerfile: {error}")
    mounts: list[Mount] = [
        Bind(PACKAGE / "debian" / "passwd", "/etc/passwd"),
        Bind(PACKAGE / "debian" / "group", "/etc/group"),
    ]
    for copy in dockerfile.image.copies:
        stage = dockerfile.stage(copy.stage)
        for source in copy.sources:
            host_path = stage_source(service, tools.gluon, stage, source)
            if not host_path.exists():
                return Unavailable(image, f"not built: {host_path} (gluon-kube build {name})")
            mounts.append(Bind(host_path, copy.dest))
    config = dockerfile.config()
    if config.working_dir != "/" and config.working_dir not in {m.target for m in mounts}:
        # Docker creates the WORKDIR (/app, with nothing copied into it for most ports).
        mounts.append(Tmpfs(config.working_dir))
    user = (config.user or "0").split(":")
    uid = int(user[0]) if user[0].isdigit() else 0
    gid = int(user[1]) if len(user) > 1 and user[1].isdigit() else uid
    return Recipe(
        image=image,
        config=config,
        uid=uid,
        gid=gid,
        home="/",
        mounts=tuple(mounts),
        differences=(
            f"built by gluon-kube build on the sandbox's toolchain, not by `docker build` "
            f"(services/{name}/Dockerfile read for its config and copies)",
            "the sandbox's root (Ubuntu), not debian:bookworm-slim's; /etc/passwd and "
            "/etc/group are the image's",
            "the WORKDIR is an empty tmpfs (writable), where Docker makes a directory",
        ),
    )


RECIPES = {
    "postgres:16-alpine": postgres,
    "redis:7-alpine": redis,
    "apache/kafka:3.8.0": kafka,
    "debezium/connect:3.0.0.Final": connect,
    "curlimages/curl:8.10.1": curl,
    "curlimages/curl:8.11.1": curl,
}


def recipe_for(image: str, tools: HostTools) -> Recipe | Unavailable:
    """The recipe for `image`, or why there is none (`ImageUnavailable`)."""
    if image == HOST_IMAGE:
        return host(image, tools)
    make = RECIPES.get(image)
    if make is None and image.split(":", 1)[0].endswith("-rs"):
        return rust_service(image, tools)
    if make is None:
        return Unavailable(image, f"no gluon-kube recipe for {image}")
    return make(image, tools)


def recipes() -> Sequence[str]:
    """The images with a recipe."""
    return sorted(RECIPES)
