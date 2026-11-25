"""Waking the box: a wake-on-LAN magic packet (six 0xFF bytes, then the network card's address 16
times) sent as a UDP broadcast. The box must be on the same network, have its network card armed for
it (the box's agent does that; see wake_on_lan in dodobe_agent.py), and allow it in its firmware.
"""

import ipaddress
import socket

from config import normal_mac

PORTS = (9, 7)


def packet(mac: str) -> bytes:
    mac = normal_mac(mac)
    if not mac:
        raise ValueError("not a network card address")
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


def targets(box_ip: str | None, extra: list[str] | None = None) -> list[str]:
    """Where to send it: everyone on this network (255.255.255.255), and the box's own network's
    broadcast address, assuming the usual home network size (/24), which routers pass on more reliably."""
    out = ["255.255.255.255"]
    try:
        ip = ipaddress.IPv4Address(box_ip or "")
        out.append(str(ipaddress.IPv4Network(f"{ip}/24", strict=False).broadcast_address))
    except ValueError:
        pass
    for x in extra or []:
        if x not in out:
            out.append(x)
    return out


def send(mac: str, box_ip: str | None = None, repeat: int = 3, extra: list[str] | None = None) -> int:
    """Send the magic packet a few times to every target. Returns how many were sent."""
    data, sent = packet(mac), 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            for addr in targets(box_ip, extra):
                for port in PORTS:
                    try:
                        s.sendto(data, (addr, port))
                        sent += 1
                    except OSError:
                        pass  # a target this PC can't reach (no such network): the others may work
    return sent
