"""In-memory active work tracker: in-flight automated solves and healer rounds for the dashboard."""

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal

SolveType = Literal["solve", "healer"]


@dataclass
class ActiveItem:
    id: str
    type: SolveType
    domain: str
    url: str
    started_at: float = field(default_factory=time.time)
    description: str = ""
    incident_id: int | None = None


class SolveTracker:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: dict[str, ActiveItem] = {}

    def start(
        self,
        type: SolveType,
        domain: str,
        url: str,
        description: str = "",
        incident_id: int | None = None,
    ) -> str:
        token = uuid.uuid4().hex[:12]
        self._items[token] = ActiveItem(
            id=token,
            type=type,
            domain=domain,
            url=url,
            started_at=self._clock(),
            description=description,
            incident_id=incident_id,
        )
        return token

    def update(self, token: str | None, description: str) -> None:
        if token and token in self._items:
            self._items[token].description = description

    def finish(self, token: str | None) -> None:
        if token:
            self._items.pop(token, None)

    @contextlib.contextmanager
    def track(
        self,
        type: SolveType,
        domain: str,
        url: str,
        description: str = "",
        incident_id: int | None = None,
    ) -> Iterator[str]:
        token = self.start(type, domain, url, description, incident_id)
        try:
            yield token
        finally:
            self.finish(token)

    def items(self) -> list[dict]:
        now = self._clock()
        return [
            {
                "id": it.id,
                "type": it.type,
                "domain": it.domain,
                "url": it.url,
                "description": it.description,
                "incident_id": it.incident_id,
                "started_at": it.started_at,
                "elapsed_s": max(0, int(now - it.started_at)),
            }
            for it in sorted(self._items.values(), key=lambda x: x.started_at, reverse=True)
        ]

    def count(self) -> int:
        return len(self._items)
