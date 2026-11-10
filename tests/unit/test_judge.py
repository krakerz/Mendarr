from mendarr.core.judge import judge
from mendarr.core.markers import extract_title
from mendarr.rpc import Cookie, Snapshot

CF_HEADERS = {"cf-mitigated": "challenge", "server": "cloudflare"}
CF_CHALLENGE = (
    '<html><head><title>Just a moment...</title></head><body>'
    '<div id="challenge-stage"></div><script>window._cf_chl_opt={}</script></body></html>'
)
CF_BLOCK_1020 = (
    "<html><head><title>Attention Required! | Cloudflare</title></head><body>"
    '<div id="cf-error-details"><span class="cf-error-code">1020</span>'
    "Sorry, you have been blocked</div></body></html>"
)
PLAIN = "<html><head><title>Hello</title></head><body>hi</body></html>"
TURNSTILE_LOGIN = (
    "<html><head><title>Login</title>"
    '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></head>'
    '<body><form><div class="cf-turnstile" data-sitekey="abc"></div></form></body></html>'
)
NGINX_ACCESS_DENIED = "<html><head><title>Access denied</title></head><body>no</body></html>"


def snap(html=PLAIN, title=None, headers=None, status=200, cookies=()):
    return Snapshot(
        url="https://example.com/",
        status=status,
        headers=headers or {},
        title=extract_title(html) if title is None else title,
        html=html,
        cookies=list(cookies),
    )


CLEARANCE = Cookie(name="cf_clearance", value="x", domain="example.com")


def test_challenge_headers_only():
    v = judge(snap(PLAIN, headers=CF_HEADERS, status=403), challenge_seen=False)
    assert (v.ok, v.reason) == (False, "challenge")


def test_challenge_title_only():
    v = judge(snap("<p>x</p>", title="Just a moment..."), challenge_seen=False)
    assert v.reason == "challenge"


def test_challenge_html_marker_only():
    v = judge(snap('<div id="challenge-form"></div>', title="x"), challenge_seen=False)
    assert v.reason == "challenge"


def test_challenge_page():
    assert judge(snap(CF_CHALLENGE, headers=CF_HEADERS, status=403), challenge_seen=True).reason == "challenge"


def test_block_page():
    v = judge(snap(CF_BLOCK_1020, headers={"server": "cloudflare"}, status=403), challenge_seen=False)
    assert (v.ok, v.reason) == (False, "blocked")


def test_plain_clear():
    v = judge(snap(PLAIN), challenge_seen=False)
    assert (v.ok, v.reason) == (True, "clear")


def test_no_clearance():
    assert judge(snap(PLAIN), challenge_seen=True).reason == "no_clearance"


def test_with_clearance():
    assert judge(snap(PLAIN, cookies=[CLEARANCE]), challenge_seen=True).ok


def test_turnstile_login_is_clear():
    assert judge(snap(TURNSTILE_LOGIN), challenge_seen=False).ok


def test_nginx_access_denied_is_clear():
    assert judge(snap(NGINX_ACCESS_DENIED, status=403), challenge_seen=False).ok


def test_access_denied_with_cloudflare_is_blocked():
    v = judge(snap(NGINX_ACCESS_DENIED, headers={"server": "cloudflare"}), challenge_seen=False)
    assert v.reason == "blocked"


def test_extract_title():
    assert extract_title("<title>\n  A \n b </title>") == "A b"
    assert extract_title("<p>none</p>") == ""
