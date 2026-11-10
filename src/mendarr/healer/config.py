"""Healer settings from <data_dir>/mendarr.yml; ${VAR} in strings is read from the environment."""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from mendarr.config import Settings

log = logging.getLogger(__name__)
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_duration(v: object) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}: use 90, 90s, 5m, 2h or 1d")
    return float(m.group(1)) * _UNIT[m.group(2)]


Duration = Annotated[float, BeforeValidator(parse_duration)]


def _expand(obj: object) -> object:
    if isinstance(obj, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [_expand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    return obj


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in the YAML should be loud


class LlmProfile(_Model):
    base_url: str  # OpenAI-compatible root, e.g. http://host:1234/v1
    model: str = ""  # exact id, else the first listed id containing it (case-insensitive); "" = first listed
    api_key: str = ""
    vision: bool = False  # send the trace screenshot
    temperature: float | None = 1.0  # Gemma vendor sampling; Gemma loops at low temperatures
    top_p: float | None = 0.95
    top_k: int | None = 64
    reasoning_effort: str | None = None  # LM Studio: "none" turns Gemma 4 thinking off
    max_tokens: int = 8192
    timeout_s: Duration = 600
    dom_chars: int = 24_000  # trimmed DOM budget in the prompt


class ManualPolicy(_Model):
    notify: list[Literal["discord", "webhook"]] = ["discord", "webhook"]
    hold_requests: bool = False  # M4


class Escalation(_Model):
    auto_attempts: int = Field(2, ge=0, le=2)  # 1 = replay as-is, 2 = also with the other backend
    heal_rounds: int = Field(3, ge=1)
    heal_windows: list[Duration] = [300, 1800, 7200]  # delay before each further window
    agent_tier: bool = False  # YAML off/on
    agent_wait: Duration = 21600
    manual: ManualPolicy = ManualPolicy()


class NotifyEvents(_Model):
    healed: bool = True
    manual: bool = True
    rollback: bool = True
    default_proposed: bool = True
    gave_up: bool = False
    agent_wanted: bool = True  # only fires with escalation.agent_tier on


class NotifyConfig(_Model):
    discord_webhook: str = ""
    webhook_url: str = ""
    events: NotifyEvents = NotifyEvents()
    timeout_s: Duration = 10


class HealerTuning(_Model):
    enabled: bool = True
    poll_s: Duration = 10
    first_delay_s: Duration = 0
    llm_wait_s: Duration = 60  # no endpoint reachable -> look again after this
    daily_rounds: int = 12  # LLM rounds per domain per local day
    trace_keep: int = 20
    trace_min_interval_s: Duration = 30
    give_up_after: Duration = 604800
    cross_replay_max: int = 5
    replay_timeout_s: Duration = 60


class LlmConfig(_Model):
    order: list[str] = []
    profiles: dict[str, LlmProfile] = {}

    @model_validator(mode="after")
    def _known(self) -> "LlmConfig":
        missing = [n for n in self.order if n not in self.profiles]
        if missing:
            raise ValueError(f"llm.order names unknown profiles: {missing}")
        return self


class DomainOverride(_Model):
    enabled: bool | None = None
    escalation: dict = {}  # any Escalation fields
    llm_order: list[str] | None = None
    daily_rounds: int | None = None
    trace_keep: int | None = None

    @model_validator(mode="after")
    def _escalation_keys(self) -> "DomainOverride":
        bad = set(self.escalation) - set(Escalation.model_fields)
        if bad:
            raise ValueError(f"unknown escalation keys: {sorted(bad)}")
        return self


@dataclass(frozen=True)
class DomainPolicy:
    enabled: bool
    escalation: Escalation
    llm_order: list[str]
    daily_rounds: int
    trace_keep: int


class HealerConfig(_Model):
    healer: HealerTuning = HealerTuning()
    escalation: Escalation = Escalation()
    llm: LlmConfig = LlmConfig()
    notify: NotifyConfig = NotifyConfig()
    domains: dict[str, DomainOverride] = {}

    def for_domain(self, domain: str) -> DomainPolicy:
        o = self.domains.get(domain) or DomainOverride()
        esc = Escalation.model_validate({**self.escalation.model_dump(), **o.escalation})
        return DomainPolicy(
            enabled=self.healer.enabled if o.enabled is None else o.enabled,
            escalation=esc,
            llm_order=o.llm_order if o.llm_order is not None else self.llm.order,
            daily_rounds=o.daily_rounds if o.daily_rounds is not None else self.healer.daily_rounds,
            trace_keep=o.trace_keep if o.trace_keep is not None else self.healer.trace_keep,
        )


def config_path(settings: Settings) -> Path:
    return Path(settings.config_file) if settings.config_file else Path(settings.data_dir) / "mendarr.yml"


def parse(text: str) -> HealerConfig:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("top level must be a mapping")
    return HealerConfig.model_validate(_expand(data))


class ConfigFile:
    """Reloads when the file's mtime changes; a broken file keeps the last good config."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._config = HealerConfig()

    def get(self) -> HealerConfig:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime is not None:
                log.warning("%s is gone; keeping the last config", self.path)
            return self._config
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._config = parse(self.path.read_text(encoding="utf-8"))
                log.info("loaded %s", self.path)
            except (OSError, ValueError, yaml.YAMLError, ValidationError) as e:
                log.error("%s is invalid, keeping the previous config: %s", self.path, e)
        return self._config
