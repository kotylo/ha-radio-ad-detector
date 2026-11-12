"""Validation for user-supplied station URLs, including redirected targets."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress

from aiohttp.resolver import DefaultResolver
from yarl import URL


def check_address(address, allow_private=False):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.is_multicast or ip.is_unspecified or (not allow_private and not ip.is_global):
        raise ValueError("Custom streams must use public internet addresses")


def validate_stream_url(value, allow_private=False):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("Supply a full HTTP or HTTPS station URL")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Station URLs cannot contain control characters")
    try:
        url = URL(value)
        if url.scheme not in ("http", "https") or not url.host or url.user is not None or url.fragment:
            raise ValueError("Use HTTP/HTTPS without embedded credentials or fragments")
        if url.port is None or not 1 <= url.port <= 65535:
            raise ValueError("Invalid station port")
        host = url.host
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid HTTP/HTTPS station URL") from exc
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        if not allow_private and (host.lower() == "localhost" or host.lower().endswith((".localhost", ".local"))):
            raise ValueError("Custom streams must use public internet addresses")
    else:
        check_address(host, allow_private)
    return url


def stream_label(url):
    """Identify a station without exposing URL query tokens in logs or sensors."""
    digest = hashlib.sha256(str(url).encode()).hexdigest()[:12]
    return f"custom:{url.host}:{digest}"


def stream_signature(secret, url, bitrate=None, output_format="mp3", slot=None):
    rate = "" if bitrate is None else format(float(bitrate), "g")
    payload = str(url) + "\n" + rate
    # Preserve existing MP3 links, but bind converted output to its signature.
    if output_format != "mp3":
        payload += "\n" + output_format
    if slot is not None:
        payload += f"\nslot={slot}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class StreamResolver(DefaultResolver):
    def __init__(self, allow_private=False):
        super().__init__()
        self.allow_private = allow_private

    async def resolve(self, host, port=0, family=0):
        results = await super().resolve(host, port, family)
        for result in results:
            check_address(result["host"], self.allow_private)
        return results
