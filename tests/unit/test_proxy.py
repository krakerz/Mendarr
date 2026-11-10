import pytest

from mendarr.core.classify import classify
from mendarr.proxy import ProxyConfigError, curl_proxy_url, describe_failure, parse_upstream, redact
from mendarr.rpc import ProxySpec, WorkerError


def test_type_param():
    assert str(parse_upstream("192.168.1.5:9999", "socks5")) == "socks5://192.168.1.5:9999"
    assert str(parse_upstream("192.168.1.5:9999", "")) == "http://192.168.1.5:9999"
    assert str(parse_upstream("h:1", "SOCKS5")) == "socks5://h:1"
    assert str(parse_upstream("h:1", "socks5h")) == "socks5://h:1"


def test_full_url_and_type_conflict():
    assert str(parse_upstream("socks5://h:1080", "")) == "socks5://h:1080"
    assert str(parse_upstream("socks5://h:1080", "socks5")) == "socks5://h:1080"
    with pytest.raises(ProxyConfigError, match="socks5.*http"):
        parse_upstream("socks5://h:1080", "http")


def test_credentials_in_url():
    p = parse_upstream("http://u:p%40ss@h:3128", "")
    assert (p.username, p.password) == ("u", "p@ss")
    assert p.playwright() == {"server": "http://h:3128", "username": "u", "password": "p@ss"}
    assert p.httpx_url() == "http://u:p%40ss@h:3128"
    assert "p@ss" not in repr(p) + str(p) + f"{p}"


def test_credentials_in_params():
    p = parse_upstream("h:3128", "", "u", "pw")
    assert (p.username, p.password) == ("u", "pw")
    assert p.httpx_url() == "http://u:pw@h:3128"
    assert "pw" not in repr(p)


def test_credential_errors():
    with pytest.raises(ProxyConfigError):
        parse_upstream("http://u:p@h:1", "", "u2", "p2")
    with pytest.raises(ProxyConfigError):
        parse_upstream("h:1", "", "", "pw")


def test_socks5_credentials_refused():
    with pytest.raises(ProxyConfigError, match="SOCKS5"):
        parse_upstream("socks5://u:p@h:1", "")
    with pytest.raises(ProxyConfigError, match="SOCKS5"):
        parse_upstream("h:1", "socks5", "u", "p")


@pytest.mark.parametrize(
    "args",
    [("h",), ("h:abc",), ("ftp://h:1",), ("h:1", "socks4"), ("http://h:1/path",), ("", "socks5")],
)
def test_invalid(args):
    with pytest.raises(ProxyConfigError):
        parse_upstream(*args)


def test_empty_is_none():
    assert parse_upstream("") is None


def test_error_hides_password():
    with pytest.raises(ProxyConfigError) as e:
        parse_upstream("http://u:secret@h", "")
    assert "secret" not in str(e.value)


def test_ipv6():
    assert parse_upstream("[::1]:1080", "socks5").server == "socks5://[::1]:1080"


def test_curl_proxy_url():
    assert curl_proxy_url(ProxySpec(url="socks5://h:1")) == "socks5h://h:1"
    assert curl_proxy_url(ProxySpec(url="http://h:1", username="u", password="p")) == "http://u:p@h:1"
    assert curl_proxy_url(ProxySpec(url="socks5h://h:1")) == "socks5h://h:1"


def test_redact():
    assert redact("http://u:p@h:1") == "http://h:1"
    assert redact("h:1") == "h:1"


@pytest.mark.parametrize(
    "detail",
    [
        "NS_ERROR_PROXY_CONNECTION_REFUSED",
        "net::ERR_PROXY_CONNECTION_FAILED",
        "net::ERR_SOCKS_CONNECTION_FAILED",
        "Failed to perform, curl: (7) Failed to connect to up port 1080 after 0 ms",
    ],
)
def test_describe_failure_names_proxy(detail):
    out = describe_failure(detail, ProxySpec(url="socks5://up:1080"))
    assert out.startswith("Upstream proxy socks5://up:1080 failed: ")
    err = WorkerError(type="navigation", detail=out)
    assert classify(error=err, snapshot=None, verdict=None) == "network"


def test_describe_failure_passthrough():
    spec = ProxySpec(url="socks5://up:1080")
    assert describe_failure("NS_ERROR_CONNECTION_REFUSED", spec) == "NS_ERROR_CONNECTION_REFUSED"
    assert describe_failure("NS_ERROR_PROXY_CONNECTION_REFUSED", None) == "NS_ERROR_PROXY_CONNECTION_REFUSED"


def test_describe_failure_hides_credentials():
    out = describe_failure("proxy down", ProxySpec(url="http://u:secret@p:3128"))
    assert out.startswith("Upstream proxy http://p:3128 failed")
    assert "secret" not in out
