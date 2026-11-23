"""Shared test setup.

The hook server's calls (``/hooks/turn/{event}``) are off for every test unless it turns them on: the capture tests'
fake gateways record whatever POST arrives, and the hook server's request is not what they are about.
``test_turn_hooks.py`` turns it on for its own runs.
"""
import pytest


@pytest.fixture(autouse=True)
def _hook_server_off(monkeypatch):
    monkeypatch.setenv("WHALES_TURN", "off")
