"""Discover the server's current IPv4 addresses without requiring internet access."""
import ipaddress
import os
import re
import socket
import subprocess
from urllib.parse import urlsplit


def usable_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
        return not (address.is_loopback or address.is_unspecified or address.is_multicast or address.is_link_local)
    except ipaddress.AddressValueError:
        return False


def parse_ipconfig(output):
    addresses, adapter = [], "Сетевой адаптер"
    for line in output.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            adapter = line.strip().rstrip(":")
        match = re.search(r"IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)", line, re.IGNORECASE)
        if match and usable_ipv4(match[1]):
            addresses.append({"ip": match[1], "name": adapter})
    return addresses


def local_addresses():
    addresses = []
    if os.name == "nt":
        try:
            result = subprocess.run([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "ipconfig.exe")], capture_output=True, text=True, encoding="oem", errors="replace", timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            addresses = parse_ipconfig(result.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            ip = item[4][0]
            if usable_ipv4(ip) and not any(a["ip"] == ip for a in addresses):
                addresses.append({"ip": ip, "name": "Локальный IPv4"})
    except OSError:
        pass
    preferred = None
    try:
        # UDP connect only asks the OS for a route; no packet is sent.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            preferred = sock.getsockname()[0]
    except OSError:
        pass
    # VPNs may own the default route but cannot be reached by phones on Wi-Fi.
    # Keep all adapters selectable, with identifiable virtual adapters last.
    virtual = re.compile(r"vpn|virtual|happ|xray|wintun|wireguard|tailscale|zerotier|hyper-v|vbox|vmware|неизвестный адаптер|unknown adapter", re.IGNORECASE)
    return sorted({a["ip"]: a for a in addresses}.values(), key=lambda a: (bool(virtual.search(a["name"])), a["ip"] != preferred, a["name"], a["ip"]))


def public_url(ip, port):
    if not usable_ipv4(ip) or not 1 <= int(port) <= 65535:
        raise ValueError("Выберите действительный LAN IPv4 и порт")
    return f"http://{ip}:{int(port)}"


def validate_manual_url(url):
    parsed = urlsplit(url.strip())
    if parsed.scheme != "http" or not parsed.hostname or not usable_ipv4(parsed.hostname) or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Укажите адрес компьютера вида http://172.17.18.194:8501 без кода и пути")
    return public_url(parsed.hostname, parsed.port or 80)
