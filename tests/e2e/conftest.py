"""E2E fixtures: need the test-env compose stack; skipped unless MENDARR_E2E_URL is set."""

import httpx
import pytest
from e2e_env import API, FIXTURE_HOST


def pytest_collection_modifyitems(config, items):
    if API:
        return
    skip = pytest.mark.skip(reason="MENDARR_E2E_URL not set")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def reset_fixture(request):
    """Forget all fixture clearances so a cached cookie is rejected (exercises the drop path)."""
    if API and "e2e" in request.keywords:
        httpx.post(f"{FIXTURE_HOST}/reset", timeout=10)
    yield
