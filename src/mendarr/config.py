from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mendarr.proxy import UpstreamProxy, parse_upstream


def _env(name: str) -> AliasChoices:
    # MENDARR_ name first (wins), the bare name too: PROXY_URL is what people write in compose files
    return AliasChoices(f"MENDARR_{name}", name)


class Settings(BaseSettings):
    # populate_by_name: Settings(proxy_url=...) still works next to the env aliases
    model_config = SettingsConfigDict(env_prefix="MENDARR_", populate_by_name=True)

    host: str = "0.0.0.0"
    port: int = 8191
    worker_url: str = "http://mendarr-browser:8192"
    slots: int = 2
    data_dir: str = "/data"
    log_level: str = "info"

    # API
    cookie_cache: bool = True
    # 0 or "auto" = no cap: an entry lives as long as its cf_clearance cookie
    cookie_max_ttl_hours: float = 6.0
    # background re-check of cached cookies: "6h", "90m", "1h"; empty or 0 = off (mendarr.yml `cookies:` overrides both)
    cookie_refresh_interval: str = ""
    cookie_refresh_idle_days: float = 7.0
    # FlareSolverr always returns {} here; real headers (Content-Encoding etc.) can confuse clients
    return_headers: bool = False

    # worker
    cookie_check: Literal["curl", "browser"] = "curl"
    curl_impersonate: str = "firefox"
    egress_check_url: str = "https://api.ipify.org?format=json"
    egress_ttl_seconds: float = 60.0
    # country lookup for the exit IP ({ip} is substituted); empty = no country lookup
    egress_country_url: str = "https://api.country.is/{ip}"
    geoip: bool = True
    click_backend: Literal["auto", "xdotool", "mouse"] = "auto"

    # worker: upstream proxy for all outbound traffic (browsers, cookie check, egress lookup); empty = direct
    proxy_url: str = Field("", validation_alias=_env("PROXY_URL"))
    proxy_type: str = Field("", validation_alias=_env("PROXY_TYPE"))  # http (default) | https | socks5
    proxy_username: str = Field("", validation_alias=_env("PROXY_USERNAME"))
    proxy_password: str = Field("", validation_alias=_env("PROXY_PASSWORD"))

    # API <-> worker: bearer token the worker requires (empty = worker accepts anyone, dev only)
    worker_token: str = ""

    # plugin stats / auto-rollback (API)
    rollback_enabled: bool = True
    rollback_min_samples: int = 5
    rollback_threshold: float = 0.5  # success rate below this rolls the active version back
    rollback_window: int = 20  # recent runs per version that count

    # worker: plugin code the asyncio deadline can't stop
    plugin_stall_s: float = 5.0  # event loop blocked this long while a plugin runs -> PluginStalled
    plugin_kill_s: float = 20.0  # still stuck (or this long past the deadline) -> worker exits

    # healer (API); everything nested (LLM profiles, budgets, notifications, per-domain) is in the YAML file
    healer_enabled: bool = True
    config_file: str = ""  # empty = <data_dir>/mendarr.yml
    public_url: str = ""  # dashboard base URL for links in notifications, e.g. http://mendarr.lan:8191

    # dashboard (/ui); no user/password = every login is refused
    ui_user: str = ""
    ui_password: str = ""
    ui_secret: str = ""  # signs session cookies; empty = random, kept in <data_dir>/ui_secret
    ui_session_hours: float = 168.0

    # MCP (/mcp): tier-2 healing by external agents; no token = MCP is off
    mcp_token: str = ""  # one bearer token, or several comma-separated (one per agent, each rate-limited)
    mcp_replays_per_hour: int = 30  # test_plugin + submit_plugin replays per token; 0 = unlimited

    @field_validator("cookie_max_ttl_hours", mode="before")
    @classmethod
    def _auto_ttl(cls, v: object) -> object:
        return 0 if isinstance(v, str) and v.strip().lower() in ("", "auto") else v

    @property
    def upstream(self) -> UpstreamProxy | None:
        """Parsed default proxy; raises ProxyConfigError (validated here, not by pydantic, whose errors echo every field)."""
        return parse_upstream(self.proxy_url, self.proxy_type, self.proxy_username, self.proxy_password)
