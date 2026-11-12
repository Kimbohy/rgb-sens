import pytest

from rgb_sens import device


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """The short pause between 'white' and 'start the firmware effect' is pointless in tests."""
    monkeypatch.setattr(device, "PRIME_DELAY", 0.0)
