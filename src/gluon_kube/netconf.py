# SPDX-License-Identifier: MIT

"""`python -m gluon_kube.netconf PID INTERFACE IP`: the pod side of a veth pair, run as
a child of the node. Joins PID's network namespace, renames INTERFACE to eth0 with IP,
brings it and lo up and routes through the node (GK-1.2)."""

import os
import sys

from pyroute2 import IPRoute  # type: ignore[import-untyped]  # ships no type information

from gluon_kube.pods import NODE_IP, POD_NETWORK


def configure(pid: int, interface: str, ip: str) -> None:
    """Configures the pod's end of its veth pair from inside its network namespace."""
    os.setns(os.open(f"/proc/{pid}/ns/net", os.O_RDONLY), os.CLONE_NEWNET)
    ipr = IPRoute()
    index = ipr.link_lookup(ifname=interface)[0]
    ipr.link("set", index=index, ifname="eth0")
    ipr.addr("add", index=index, address=ip, prefixlen=POD_NETWORK.prefixlen)
    ipr.link("set", index=index, state="up")
    ipr.link("set", index=ipr.link_lookup(ifname="lo")[0], state="up")
    ipr.route("add", dst="0.0.0.0/0", gateway=NODE_IP)


if __name__ == "__main__":
    configure(int(sys.argv[1]), sys.argv[2], sys.argv[3])
