"""Give the Patchright browser the same DNS answers the HTTP session gets.

The niquests sessions resolve through DNS over HTTPS, but Chromium asks the
system resolver. Some German ISPs answer for blocked streaming sites (CUII) with
the address of a block page, so the browser lands on a host serving a
certificate for something like cuii.telefonica.de and every navigation fails
with ERR_CERT_COMMON_NAME_INVALID while plain requests work fine.

The site hosts are resolved over DoH once and pinned with Chromium's
--host-resolver-rules. Only the IP is overridden, so the TLS name check still
runs against the real host name.
"""

import ipaddress
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from niquests import Session

from ..config import GLOBAL_SESSION, STO_DOMAINS, STO_IP
from ..logger import get_logger

logger = get_logger(__name__)

# Hosts of the sites the browser navigates to. Hosters (VOE, Doodstream, ...)
# are not on the CUII list and keep resolving normally.
SITE_HOSTS = (
    *STO_DOMAINS,
    "aniworld.to",
    "filmpalast.to",
    "moflix-stream.xyz",
    "moflix-stream.click",
    "bs.to",
    "burningseries.to",
    "kinox.to",
)

# Queried by IP, so looking them up needs no working DNS
_DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
)

_TTL_SECONDS = 30 * 60
_cache = {}
_cache_lock = threading.Lock()


def _enabled():
    return os.getenv("ANIWORLD_BROWSER_DOH", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _hosts():
    extra = os.getenv("ANIWORLD_BROWSER_DOH_HOSTS", "")
    hosts = [*SITE_HOSTS, *(h.strip().lower() for h in extra.split(","))]
    return list(dict.fromkeys(host for host in hosts if host))


def _query(session, host):
    """First IPv4 address for host from a DoH endpoint, or None."""
    for endpoint in _DOH_ENDPOINTS:
        try:
            response = session.get(
                endpoint,
                params={"name": host, "type": "A"},
                headers={"Accept": "application/dns-json"},
                timeout=5,
            )
            response.raise_for_status()
            for answer in response.json().get("Answer") or ():
                if answer.get("type") != 1:
                    continue
                address = str(answer.get("data", "")).strip()
                try:
                    ipaddress.IPv4Address(address)
                except ValueError:
                    continue
                return address
        except Exception as exc:
            logger.debug("DoH lookup of %s via %s failed: %s", host, endpoint, exc)
    return None


def _resolve_all(hosts):
    now = time.monotonic()
    fresh = {}
    with _cache_lock:
        for host in hosts:
            entry = _cache.get(host)
            if entry and now - entry[1] < _TTL_SECONDS:
                fresh[host] = entry[0]
    missing = [host for host in hosts if host not in fresh]
    if missing:
        session = Session()
        session.verify = GLOBAL_SESSION.verify
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
                found = dict(
                    zip(missing, pool.map(lambda h: _query(session, h), missing))
                )
        finally:
            session.close()
        for host, address in found.items():
            if address is None and host in STO_DOMAINS:
                # The known serienstream address, the same last resort http.py uses
                address = STO_IP
            if address is not None:
                fresh[host] = address
                with _cache_lock:
                    _cache[host] = (address, now)
    return fresh


def host_resolver_rules():
    """The value for Chromium's --host-resolver-rules, or None to leave DNS alone."""
    if not _enabled():
        return None
    try:
        resolved = _resolve_all(_hosts())
    except Exception as exc:
        logger.warning("Could not resolve site hosts over DoH: %s", exc)
        return None
    rules = []
    for host, address in resolved.items():
        rules.append(f"MAP {host} {address}")
        rules.append(f"MAP www.{host} {address}")
    return ", ".join(rules) or None


def browser_dns_args():
    """Extra Chromium launch arguments that pin the site hosts to DoH answers."""
    rules = host_resolver_rules()
    return [f"--host-resolver-rules={rules}"] if rules else []
