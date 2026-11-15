"""Security helpers shared across authentication and request handling."""

import ipaddress
import os


_TRUSTED_INTERNAL_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy(remote_addr: str) -> bool:
    """
    Returns True only when REMOTE_ADDR belongs to a trusted local/internal reverse proxy
    (loopback, RFC 1918 private proxy range, or DJANGO_TRUSTED_PROXIES).
    """
    if not remote_addr:
        return False
    configured = {
        ip.strip()
        for ip in os.environ.get("DJANGO_TRUSTED_PROXIES", "").split(",")
        if ip.strip()
    }
    if configured:
        return remote_addr in configured
    try:
        addr = ipaddress.ip_address(remote_addr)
        return any(addr in net for net in _TRUSTED_INTERNAL_NETWORKS)
    except ValueError:
        return False


def get_client_ip(request):
    """
    Real client IP when running behind a trusted reverse proxy (PythonAnywhere sets X-Real-IP),
    falling back to REMOTE_ADDR when accessed directly or if X-Real-IP is malformed/untrusted.
    """
    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    if _is_trusted_proxy(remote_addr):
        forwarded = (request.META.get("HTTP_X_REAL_IP") or "").strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return remote_addr
