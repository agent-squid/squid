"""Shared outbound API URL boundary, independent of server configuration."""
import ipaddress
from urllib.parse import urlsplit


def valid_api_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {'', '/'}
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return False
        if parsed.scheme == 'https' or parsed.hostname == 'localhost':
            return True
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False
