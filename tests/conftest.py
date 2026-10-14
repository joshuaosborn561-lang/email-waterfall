from __future__ import annotations

import pytest

from email_waterfall.vendors.getleads import reset_getleads_pace


@pytest.fixture(autouse=True)
def _reset_getleads_pace() -> None:
    reset_getleads_pace()
