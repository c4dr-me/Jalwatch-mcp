"""Smoke tests for the installed project package."""

from importlib.metadata import distribution

import jalwatch


def test_package_is_installed() -> None:
    """Check package import and installed distribution metadata together."""
    assert jalwatch.__name__ == "jalwatch"
    assert distribution("jalwatch").metadata["Name"] == "jalwatch"
