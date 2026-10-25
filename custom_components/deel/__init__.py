"""Deel for workers: balance, contracts, invoices, withdrawals and time off."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import DeelCoordinator
from .session import create_client

PLATFORMS: list[Platform] = [Platform.SENSOR]

type DeelConfigEntry = ConfigEntry[DeelCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DeelConfigEntry) -> bool:
    """Reuse the saved token (or log in) and start polling."""
    client, http = create_client(hass)
    entry.async_on_unload(http.detach)

    coordinator = DeelCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DeelConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
