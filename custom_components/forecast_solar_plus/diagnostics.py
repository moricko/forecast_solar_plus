"""Diagnostika integrácie – export stavu pre ladenie (bez API kľúča)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY
from .coordinator import ForecastSolarPlusConfigEntry

# Citlivé polia, ktoré sa v diagnostike nahradia zástupným textom.
_REDACT = {CONF_API_KEY, "latitude", "longitude"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ForecastSolarPlusConfigEntry
) -> dict[str, Any]:
    """Vráti konfiguráciu záznamu a súhrn aktuálnych dát koordinátora."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), _REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "update_interval_minutes": coordinator.update_interval_minutes,
            "has_api_key": coordinator.api.has_api_key,
            "inverter_kw": coordinator.inverter_kw,
            "last_update_success": coordinator.last_update_success,
        },
        "data": None
        if data is None
        else {
            "polled_at": data.polled_at.isoformat(),
            "from_cache": data.from_cache,
            "last_error": data.last_error,
            "place": data.place,
            "ratelimit": data.ratelimit.as_dict() if data.ratelimit else None,
            "combined": data.combined.serialize(),
            "planes": {plane_id: curve.serialize() for plane_id, curve in data.planes.items()},
        },
    }
