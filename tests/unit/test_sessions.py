from mendarr.rpc import ProxySpec
from mendarr.worker.sessions import SessionManager


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.closed = False
        self.proxy = None

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.connected = True
        self.contexts: list[FakeContext] = []

    def is_connected(self):
        return self.connected

    async def new_context(self, proxy=None):
        c = FakeContext(self)
        c.proxy = proxy
        self.contexts.append(c)
        return c


class FakePool:
    def __init__(self, count=2):
        self.browsers = {i: FakeBrowser() for i in range(1, count + 1)}

    @property
    def indexes(self):
        return list(self.browsers)

    async def browser(self, slot):
        return self.browsers[slot]


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


async def test_create_fresh_then_existing():
    m = SessionManager(FakePool())
    s, fresh = await m.create("a")
    s2, fresh2 = await m.create("a")
    assert fresh and not fresh2 and s is s2


async def test_default_id_is_uuid():
    m = SessionManager(FakePool())
    s, _ = await m.create(None)
    assert len(s.id) == 36


async def test_slot_balancing():
    m = SessionManager(FakePool(2))
    slots = [(await m.create(f"s{i}"))[0].slot for i in range(4)]
    assert slots == [1, 2, 1, 2]


async def test_proxy_passed_to_context():
    pool = FakePool(1)
    m = SessionManager(pool)
    await m.create("a", ProxySpec(url="http://p:1", username="u", password="x"))
    assert pool.browsers[1].contexts[0].proxy == {"server": "http://p:1", "username": "u", "password": "x"}


async def test_ttl_recreates():
    clock = Clock()
    m = SessionManager(FakePool(1), clock)
    first = await m.get_or_create("a", 10, None)
    clock.t = 599
    assert await m.get_or_create("a", 10, None) is first
    clock.t = 601
    second = await m.get_or_create("a", 10, None)
    assert second is not first and first.context.closed


async def test_recreate_when_browser_gone():
    pool = FakePool(1)
    m = SessionManager(pool)
    first = await m.get_or_create("a", None, None)
    pool.browsers[1].connected = False
    pool.browsers[1].connected = True
    first.context.browser = None
    assert await m.get_or_create("a", None, None) is not first


async def test_destroy_and_ids():
    m = SessionManager(FakePool())
    s, _ = await m.create("a")
    assert m.ids() == ["a"]
    assert await m.destroy("a") is True
    assert await m.destroy("a") is False
    assert s.context.closed and m.ids() == []
