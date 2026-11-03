from __future__ import annotations

import pytest

from nseswing.config import Settings, load_settings
from nseswing.core.calendar import NSECalendar
from nseswing.core.ticks import TickTable


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def calendar(settings: Settings) -> NSECalendar:
    return NSECalendar.from_settings(settings)


@pytest.fixture(scope="session")
def ticks(settings: Settings) -> TickTable:
    return TickTable.from_settings(settings)


def make_settings(**overrides) -> Settings:
    """Settings with nested overrides, e.g. make_settings(risk={'stop_method': 'prior_bar_low'})."""
    return load_settings(overrides=overrides)
