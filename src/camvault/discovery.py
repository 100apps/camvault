from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from uuid import uuid4
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from camvault.onvif import local_name

MULTICAST_ADDRESS = ("239.255.255.250", 3702)
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]

    @property
    def primary_xaddr(self) -> str:
        return self.xaddrs[0] if self.xaddrs else ""


def build_probe_message() -> bytes:
    soap = "http://www.w3.org/2003/05/soap-envelope"
    wsa = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
    discovery = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
    device_network = "http://www.onvif.org/ver10/network/wsdl"

    envelope = ET.Element(ET.QName(soap, "Envelope"))
    header = ET.SubElement(envelope, ET.QName(soap, "Header"))
    ET.SubElement(header, ET.QName(wsa, "MessageID")).text = f"uuid:{uuid4()}"
    ET.SubElement(header, ET.QName(wsa, "To")).text = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
    ET.SubElement(
        header, ET.QName(wsa, "Action")
    ).text = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
    body = ET.SubElement(envelope, ET.QName(soap, "Body"))
    probe = ET.SubElement(body, ET.QName(discovery, "Probe"))
    types = ET.SubElement(probe, ET.QName(discovery, "Types"))
    types.set("xmlns:dn", device_network)
    types.text = "dn:NetworkVideoTransmitter"
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def parse_probe_response(payload: bytes) -> list[DiscoveredDevice]:
    root = SafeET.fromstring(payload)
    devices: list[DiscoveredDevice] = []
    for match in root.iter():
        if local_name(match.tag) != "ProbeMatch":
            continue
        xaddrs: tuple[str, ...] = ()
        scopes: tuple[str, ...] = ()
        for child in match.iter():
            name = local_name(child.tag)
            if name == "XAddrs" and child.text:
                xaddrs = tuple(item for item in child.text.split() if item)
            elif name == "Scopes" and child.text:
                scopes = tuple(item for item in child.text.split() if item)
        if xaddrs:
            devices.append(DiscoveredDevice(xaddrs=xaddrs, scopes=scopes))
    return devices


def discover_onvif(timeout_seconds: float = 3.0) -> list[DiscoveredDevice]:
    """Discover ONVIF devices over IPv4 WS-Discovery.

    Discovery multicast is often blocked across VLANs, Wi-Fi client isolation, VPNs, and
    container networks. A configured camera IP remains the reliable fallback.
    """

    probe = build_probe_message()
    found: dict[tuple[str, ...], DiscoveredDevice] = {}
    deadline = time.monotonic() + timeout_seconds

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        sock.settimeout(min(0.5, timeout_seconds))
        for _ in range(2):
            sock.sendto(probe, MULTICAST_ADDRESS)

        while time.monotonic() < deadline:
            try:
                payload, _address = sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                for device in parse_probe_response(payload):
                    found[device.xaddrs] = device
            except (ET.ParseError, DefusedXmlException, ValueError) as exc:
                logger.debug("ignored malformed WS-Discovery response: %s", exc)
                continue

    return sorted(found.values(), key=lambda item: item.primary_xaddr)
