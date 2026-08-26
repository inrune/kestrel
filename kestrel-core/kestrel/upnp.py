"""
Kestrel automatic port forwarding — UPnP-IGD with NAT-PMP fallback.

Most home routers speak UPnP-IGD; Apple routers and many modern ones
speak NAT-PMP (RFC 6886). On startup — and periodically after, so
router reboots and lease expiries heal themselves — the node asks the
router to forward its TCP port to this machine, which makes the node
reachable from the public internet with zero configuration — the same
trick early Bitcoin used so ordinary home computers could accept
incoming connections. Everything here is plain standard library; if
anything fails (no router, UPnP disabled, weird firmware) the node
simply carries on, reachable outbound-only.
"""

import re
import socket
import urllib.request

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
).encode()

SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


def _discover_gateway(timeout=2.5):
    """SSDP multicast search. Returns the gateway description URL or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(SEARCH, SSDP_ADDR)
        while True:
            try:
                data, _ = s.recvfrom(2048)
            except socket.timeout:
                return None
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m:
                return m.group(1).decode()
    except OSError:
        return None
    finally:
        s.close()


def _control_url(desc_url, timeout=3):
    """Parse the device description for a WAN*Connection control URL."""
    try:
        with urllib.request.urlopen(desc_url, timeout=timeout) as r:
            xml = r.read(131072).decode("utf-8", "replace")
    except Exception:
        return None, None
    for st in SERVICE_TYPES:
        # the controlURL that follows this serviceType
        pat = (re.escape(st) +
               r".*?<controlURL>\s*([^<\s]+)\s*</controlURL>")
        m = re.search(pat, xml, re.S)
        if m:
            path = m.group(1)
            if path.startswith("http"):
                return path, st
            m2 = re.match(r"(https?://[^/]+)", desc_url)
            if m2:
                return m2.group(1) + (path if path.startswith("/")
                                      else "/" + path), st
    return None, None


def _soap(control, service, action, args, timeout=4):
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    req = urllib.request.Request(
        control, data=envelope, method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#{action}"',
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(65536).decode("utf-8", "replace")


# ---------------------------------------------------------------- NAT-PMP

NATPMP_PORT = 5351
NATPMP_LIFETIME = 7200      # seconds; the node renews well before this


def _gateway_guesses(lan_ip: str) -> list:
    """Likely router addresses. NAT-PMP talks to the default gateway; the
    standard library can't ask the OS for it portably, but on home
    networks the router is almost always .1 (sometimes .254)."""
    if not lan_ip or lan_ip.startswith("127."):
        return []
    base = lan_ip.rsplit(".", 1)[0]
    return [f"{base}.1", f"{base}.254"]


def natpmp_open(port: int, lan_ip: str, lifetime: int = NATPMP_LIFETIME):
    """RFC 6886: ask the gateway to map TCP `port` for `lifetime` seconds.

    Returns (mapped: bool, external_ip: str | None). Never raises.
    Mappings expire, so call this again periodically to renew."""
    for gw in _gateway_guesses(lan_ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.5)
        try:
            external = None
            s.sendto(b"\x00\x00", (gw, NATPMP_PORT))   # external-IP request
            data, _ = s.recvfrom(16)
            if (len(data) >= 12 and data[0] == 0 and data[1] == 128
                    and data[2:4] == b"\x00\x00"):
                external = ".".join(str(b) for b in data[8:12])
            # TCP mapping request (opcode 2)
            req = (b"\x00\x02\x00\x00" + port.to_bytes(2, "big") * 2
                   + lifetime.to_bytes(4, "big"))
            s.sendto(req, (gw, NATPMP_PORT))
            data, _ = s.recvfrom(16)
            if (len(data) >= 16 and data[0] == 0 and data[1] == 130
                    and data[2:4] == b"\x00\x00"
                    and int.from_bytes(data[10:12], "big") == port):
                return True, external
            if external:               # gateway speaks NAT-PMP but refused
                return False, external
        except OSError:
            continue
        finally:
            s.close()
    return False, None


# ----------------------------------------------------------------- public

def open_port(port: int, lan_ip: str, description="Kestrel node"):
    """Try to forward TCP `port` to `lan_ip` and learn the external IP.

    Tries UPnP-IGD first, then NAT-PMP. Returns (mapped: bool,
    external_ip: str | None). Never raises. Safe to call repeatedly —
    the node does, to renew leases and survive router reboots.
    """
    mapped, ext = _upnp_open(port, lan_ip, description)
    if mapped:
        return True, ext
    mapped2, ext2 = natpmp_open(port, lan_ip)
    return mapped2, (ext2 or ext)


def _upnp_open(port: int, lan_ip: str, description="Kestrel node"):
    """UPnP-IGD half of open_port. Returns (mapped, external_ip)."""
    try:
        desc = _discover_gateway()
        if not desc:
            return False, None
        control, service = _control_url(desc)
        if not control:
            return False, None
        external = None
        try:
            xml = _soap(control, service, "GetExternalIPAddress", [])
            m = re.search(r"<NewExternalIPAddress>\s*([^<\s]+)\s*"
                          r"</NewExternalIPAddress>", xml)
            if m:
                external = m.group(1)
        except Exception:
            pass
        try:
            _soap(control, service, "AddPortMapping", [
                ("NewRemoteHost", ""),
                ("NewExternalPort", port),
                ("NewProtocol", "TCP"),
                ("NewInternalPort", port),
                ("NewInternalClient", lan_ip),
                ("NewEnabled", "1"),
                ("NewPortMappingDescription", description),
                ("NewLeaseDuration", "0"),
            ])
            return True, external
        except Exception:
            return False, external
    except Exception:
        return False, None
