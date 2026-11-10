import pytest

from mendarr.core.domains import hostname, registrable_domain


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.b.example.co.uk/x", "example.co.uk"),
        ("http://fixture:8080/cf/pass", "fixture"),
        ("http://127.0.0.1:8180/", "127.0.0.1"),
        ("", ""),
        ("https://WWW.Example.COM./p", "example.com"),
    ],
)
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected


def test_hostname_normalised():
    assert hostname("https://Sub.Example.com./x") == "sub.example.com"
