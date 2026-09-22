"""Forecast.Solar Plus – predpoveď výroby FVE v štýle Solcast PV Forecast.

Zodpovednosť modulu: založenie/zrušenie config entry, registrácia služieb
a reakcia na zmenu možností (reload).
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_END_DATE_TIME,
    ATTR_PERIOD_START,
    ATTR_PLANE,
    ATTR_PV_ESTIMATE,
    ATTR_START_DATE_TIME,
    DOMAIN,
    FORECAST_PERIOD,
    SERVICE_FORCE_UPDATE,
    SERVICE_QUERY_FORECAST_DATA,
    STORAGE_VERSION,
)
from .coordinator import ForecastSolarPlusConfigEntry, ForecastSolarPlusCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_QUERY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START_DATE_TIME): cv.datetime,
        vol.Required(ATTR_END_DATE_TIME): cv.datetime,
        vol.Optional(ATTR_PLANE): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)
_FORCE_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Registruje služby domény (raz, nezávisle od počtu záznamov)."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_FORECAST_DATA,
        _make_query_service(hass),
        schema=_QUERY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_FORCE_UPDATE, _make_force_update_service(hass), schema=_FORCE_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ForecastSolarPlusConfigEntry) -> bool:
    """Založí koordinátor, načíta medzipamäť, spraví prvú aktualizáciu a platformy."""
    _LOGGER.info("Načítavam Forecast.Solar Plus „%s“", entry.title)
    coordinator = ForecastSolarPlusCoordinator(hass, entry)
    await coordinator.async_load_cache()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start_local_tick()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ForecastSolarPlusConfigEntry) -> bool:
    """Odstráni platformy a zastaví koordinátor."""
    _LOGGER.info("Uvoľňujem Forecast.Solar Plus „%s“", entry.title)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Pri odstránení záznamu zmaže aj jeho medzipamäť v `.storage`."""
    try:
        await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    except Exception as err:  # noqa: BLE001 – odstránenie záznamu nesmie zlyhať kvôli súboru
        _LOGGER.warning("Nepodarilo sa odstrániť medzipamäť záznamu %s: %s", entry.entry_id, err)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Po zmene možností alebo plôch sa integrácia znovu načíta."""
    _LOGGER.debug("Zmena konfigurácie „%s“ – reload", entry.title)
    await hass.config_entries.async_reload(entry.entry_id)


# ------------------------------------------------------------------- služby


def _resolve_coordinator(hass: HomeAssistant, entry_id: str | None) -> ForecastSolarPlusCoordinator:
    """Nájde koordinátor podľa id záznamu; bez id vráti jediný načítaný záznam."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id:
        entry = next((e for e in entries if e.entry_id == entry_id), None)
        if entry is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entry_not_found",
                translation_placeholders={"entry_id": entry_id},
            )
        return entry.runtime_data
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entries")
    if len(entries) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_ambiguous")
    return entries[0].runtime_data


def _make_query_service(hass: HomeAssistant):
    """Služba `query_forecast_data`: vráti 30-minútové periódy medzi dvoma časmi (ako Solcast)."""

    async def _handle(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        start: datetime = call.data[ATTR_START_DATE_TIME]
        end: datetime = call.data[ATTR_END_DATE_TIME]
        # cv.datetime môže vrátiť naivný čas – považujeme ho za lokálny čas HA.
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt_util.get_default_time_zone())
        if end.tzinfo is None:
            end = end.replace(tzinfo=dt_util.get_default_time_zone())
        start, end = dt_util.as_utc(start), dt_util.as_utc(end)
        if end <= start:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_range")

        data = coordinator.data
        plane_name = call.data.get(ATTR_PLANE)
        if plane_name:
            plane = coordinator.plane_by_name(plane_name)
            if plane is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="plane_not_found",
                    translation_placeholders={"plane": plane_name},
                )
            curve = data.planes.get(plane.id)
            if curve is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_data")
        else:
            curve = data.combined

        periods = curve.periods(start, end, FORECAST_PERIOD)
        _LOGGER.debug("query_forecast_data: %s – %s, %d periód", start.isoformat(), end.isoformat(), len(periods))
        return {
            "data": [
                {ATTR_PERIOD_START: dt_util.as_local(p.start).isoformat(), ATTR_PV_ESTIMATE: round(p.value / 1000.0, 4)}
                for p in periods
            ]
        }

    return _handle


def _make_force_update_service(hass: HomeAssistant):
    """Služba `force_update_forecasts`: vynúti volanie API mimo plánu."""

    async def _handle(call: ServiceCall) -> None:
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        targets = (
            [_resolve_coordinator(hass, entry_id)]
            if entry_id
            else [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]
        )
        if not targets:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entries")
        for coordinator in targets:
            _LOGGER.info("Vynútená aktualizácia predpovede „%s“", coordinator.config_entry.title)
            await coordinator.async_force_refresh()

    return _handle
