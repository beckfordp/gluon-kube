# SPDX-License-Identifier: MIT

"""The node's network (ADR 0030 decision 3, proven in GK-1.1 to GK-1.4): a bridge the pods
hang off, one veth pair per pod, ClusterIPs on the node's `lo`. Runs inside the node
namespace, where gluon-kube holds CAP_NET_ADMIN; configured over netlink (pyroute2), as
the sandbox has no `ip`."""

import contextlib
import subprocess
import sys

from pyroute2 import IPRoute  # type: ignore[import-untyped]  # ships no type information

from gluon_kube.pods import NODE_IP, POD_NETWORK

BRIDGE = "cbr0"
HELPER_TIMEOUT_S = 60.0
"""The pod-side helper imports pyroute2 (~1.7 s alone on a busy node, GK-1)."""


class Network:
    """The node's bridge, pod wiring and ClusterIP addresses."""

    def __init__(self) -> None:
        self.ipr = IPRoute()
        lo = self.ipr.link_lookup(ifname="lo")[0]
        self.ipr.link("set", index=lo, state="up")
        self.ipr.link("add", ifname=BRIDGE, kind="bridge")
        self.bridge = self.ipr.link_lookup(ifname=BRIDGE)[0]
        self.ipr.addr("add", index=self.bridge, address=NODE_IP, prefixlen=POD_NETWORK.prefixlen)
        self.ipr.link("set", index=self.bridge, state="up")

    def wire(self, slot: int, pid: int, ip: str) -> None:
        """A veth pair: the node's end on the bridge, the other the pod's eth0 (its IP,
        lo up, the node as its gateway), configured from a helper that joins the pod's
        network namespace (a child process: the node runs threads, GK-1)."""
        outside, inside = f"gkv{slot}", f"gkp{slot}"
        self.ipr.link("add", ifname=outside, kind="veth", peer=inside)
        self.ipr.link("set", index=self.ipr.link_lookup(ifname=inside)[0], net_ns_pid=pid)
        end = self.ipr.link_lookup(ifname=outside)[0]
        self.ipr.link("set", index=end, master=self.bridge, state="up")
        argv = [sys.executable, "-m", "gluon_kube.netconf", str(pid), inside, ip]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=HELPER_TIMEOUT_S)
        if done.returncode != 0:
            raise OSError(f"configuring pod network (pid {pid}) failed: {done.stderr.strip()}")

    def unwire(self, slot: int) -> None:
        """Removes a pod's veth pair (gone already if its namespace ended)."""
        with contextlib.suppress(Exception):
            for index in self.ipr.link_lookup(ifname=f"gkv{slot}"):
                self.ipr.link("del", index=index)

    def add_address(self, ip: str) -> None:
        """A ClusterIP on `lo`: delivered locally from any interface (this kernel has no
        `dummy` link type, GK-1.4)."""
        lo = self.ipr.link_lookup(ifname="lo")[0]
        with contextlib.suppress(Exception):  # already there
            self.ipr.addr("add", index=lo, address=ip, prefixlen=32)

    def remove_address(self, ip: str) -> None:
        """Removes a ClusterIP."""
        lo = self.ipr.link_lookup(ifname="lo")[0]
        with contextlib.suppress(Exception):
            self.ipr.addr("del", index=lo, address=ip, prefixlen=32)
