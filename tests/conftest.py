"""Real Home Assistant fixtures for integration paths."""

from __future__ import annotations

import gc
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Work around a CPython 3.14.6 crash in the interpreter-shutdown GC.

    After the full suite passes, CPython 3.14.6 (both Homebrew and
    python-build-standalone builds) segfaults during ``Py_Finalize`` —
    ``gc_collect_main`` -> ``subtype_dealloc`` -> ``PyObject_ClearManagedDict``
    (confirmed with ``PYTHONFAULTHANDLER=1``) while tearing down the large Home
    Assistant object graph left by combining the config-flow tests with a
    full-integration test file (e.g. ``test_init``/``test_cover``). Every test
    passes; only the finalization collection crashes, which still fails CI with
    exit 139. A per-test ``gc.collect()`` does not help (the survivors are held
    until shutdown). Freezing them into the permanent generation once the
    session result is known excludes them from that final collection, so the
    process exits cleanly without changing any test outcome. Remove this when
    the upstream CPython finalization bug is fixed.
    """
    del session, exitstatus
    gc.freeze()


@pytest_asyncio.fixture
async def hass(tmp_path: str) -> AsyncIterator[HomeAssistant]:
    """Run a minimal real Home Assistant core for entity and lifecycle tests."""
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    await instance.async_start()
    try:
        yield instance
    finally:
        await instance.async_stop(force=True)


@pytest.fixture(autouse=True)
def _mqtt_client_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the MQTT client as ready; integration tests stub the transport."""
    from homeassistant.components import mqtt

    async def ready(_hass: object) -> bool:
        return True

    monkeypatch.setattr(mqtt, "async_wait_for_mqtt_client", ready, raising=False)
