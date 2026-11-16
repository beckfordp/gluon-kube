# SPDX-License-Identifier: MIT

"""The installed command and `python -m gluon_kube` (GK-2.2), run as processes."""

import subprocess
import sys
from pathlib import Path

import pytest

CONSOLE_SCRIPT = Path(sys.executable).parent / "gluon-kube"


@pytest.mark.parametrize(
    "argv",
    [[str(CONSOLE_SCRIPT)], [sys.executable, "-m", "gluon_kube"]],
    ids=["console-script", "python-m"],
)
def test_version_from_a_process(argv: list[str]) -> None:
    done = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=30)
    assert (done.returncode, done.stdout) == (0, "gluon-kube 0.1.0\n")


def test_no_command_exits_2_from_a_process() -> None:
    done = subprocess.run([str(CONSOLE_SCRIPT)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 2
    assert done.stderr.startswith("usage: gluon-kube")


GLUON = Path(__file__).resolve().parents[4]
LOCAL_INFRA = GLUON / "infra" / "k8s" / "local-infra"


@pytest.mark.skipif(not LOCAL_INFRA.is_dir(), reason="needs a gluon checkout around the repo")
def test_gluon_local_infra_reads_with_nothing_unsupported() -> None:
    """gluon's own manifests, read as they are (ADR 0030 decision 2)."""
    done = subprocess.run(
        [str(CONSOLE_SCRIPT), "apply", "--dry-run", "-f", str(LOCAL_INFRA)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines()[-1].startswith("36 objects (")
    assert "UNSUPPORTED" not in done.stdout
