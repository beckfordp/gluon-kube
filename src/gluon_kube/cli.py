# SPDX-License-Identifier: MIT

"""The `gluon-kube` command. Its verbs follow kubectl's and helm's (gluon ADR 0030
decision 5): so far `apply --dry-run` (GK-3.7); `template` comes with the chart (GK-3.6);
the runtime's verbs from GK-4.

Exit codes: 0 done; 1 something unsupported; 2 a usage error or input gluon-kube
can't read (the message names the file and document).
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from gluon_kube import __version__
from gluon_kube.manifests import ManifestError, load
from gluon_kube.report import describe, summary

DESCRIPTION = (
    "A simulated gluon-local for the sandbox (gluon ADR 0030): runs gluon-local's "
    "unchanged manifests, chart and values files as local processes in unprivileged "
    "namespaces."
)


def parser() -> argparse.ArgumentParser:
    """The command line."""
    result = argparse.ArgumentParser(prog="gluon-kube", description=DESCRIPTION)
    result.add_argument("--version", action="version", version=f"gluon-kube {__version__}")
    commands = result.add_subparsers(dest="command", required=True, metavar="command")
    apply = commands.add_parser(
        "apply", help="apply manifests (--dry-run: say what would happen to each object)"
    )
    apply.add_argument(
        "-f", "--filename", action="append", type=Path, required=True, help="file or directory"
    )
    apply.add_argument("--dry-run", action="store_true", help="report only; start nothing")
    return result


def apply(files: Sequence[Path], dry_run: bool) -> int:
    """`apply`: the report for every object; 1 if anything is unsupported."""
    if not dry_run:
        print("gluon-kube: error: the runtime comes in GK-4; use --dry-run", file=sys.stderr)
        return 2
    items = load(files)
    for item in items:
        print("\n".join(describe(item)))
    print(summary(items))
    return 0 if all(item.supported for item in items) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the command; returns its exit code (argparse exits itself on --help,
    --version and usage errors)."""
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return apply(args.filename, args.dry_run)
    except ManifestError as error:
        print(f"gluon-kube: error: {error}", file=sys.stderr)
        return 2
