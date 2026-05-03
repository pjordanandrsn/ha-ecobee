"""Test fixtures for the Ecobee Anderson fork integration.

In the standalone repo layout, ``custom_components/`` sits at the repo
root. Prepend the repo root to sys.path so ``import
custom_components.ecobee`` resolves to the in-tree integration before
any test module is collected.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Place repo root on sys.path so ``import custom_components.ecobee``
# resolves to the in-tree integration. Done in conftest.py so it runs
# before any test module is imported.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integration discovery for every test."""
    yield


@pytest.fixture
def bypass_get_data():
    """Bypass the coordinator's first-refresh so async_setup_entry returns True."""

    async def mock_first_refresh(self):
        self.last_update_success = True
        self.data = []

    with patch(
        "custom_components.ecobee.coordinator.EcobeeDataUpdateCoordinator."
        "async_config_entry_first_refresh",
        mock_first_refresh,
    ):
        yield


@pytest.fixture
def error_on_get_data():
    """Force the API client to raise on every poll."""
    with patch(
        "custom_components.ecobee.api.EcobeeApiClient.async_get_thermostats",
        side_effect=Exception,
    ):
        yield
