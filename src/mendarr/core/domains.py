import ipaddress
from urllib.parse import urlsplit

import tldextract

# suffix_list_urls=() = bundled PSL snapshot; the default fetches the list over the network
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def registrable_domain(url: str) -> str:
    host = hostname(url)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    r = _extract(host)
    return r.top_domain_under_public_suffix or host
