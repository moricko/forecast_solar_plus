"""Integračné testy – reálny setup config entry v testovacom Home Assistant.

Vyžadujú `pytest-homeassistant-custom-component` (a teda nainštalovaný Home Assistant,
t. j. Linux/WSL). Volania API sú nahradené mockom, takže testy nemíňajú limit.
Bez HA sa celý modul preskočí, aby `python -m pytest tests` fungovalo aj na Windows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.forecast_solar_plus.api import PlaneEstimate, RateLimit
from custom_components.forecast_solar_plus.const import (
    ATTR_DETAILED_FORECAST,
    CONF_API_KEY,
    CONF_AZIMUTH,
    CONF_DECLINATION,
    CONF_INVERTER_KW,
    CONF_KWP,
    CONF_PLANE_ID,
    CONF_PLANE_NAME,
    CONF_PLANES,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    SERVICE_FORCE_UPDATE,
    SERVICE_QUERY_FORECAST_DATA,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Povolí načítanie custom_components v testovacom HA."""
    yield


@pytest.fixture
def frozen_now():
    """Zmrazí čas na 2026-09-21 10:15 UTC (12:15 v Bratislave), aby boli hodnoty deterministické."""
    now = datetime(2026, 9, 21, 10, 15, tzinfo=timezone.utc)
    with (
        patch("homeassistant.util.dt.utcnow", return_value=now),
        patch("homeassistant.util.dt.now", side_effect=lambda tz=None: now.astimezone(tz or dt_util.DEFAULT_TIME_ZONE)),
    ):
        yield now


def _estimate_for(azimuth: float) -> PlaneEstimate:
    """Syntetická odpoveď API: symetrický trojuholník 06–18 h, špička 2000 W o 12:00 (+02:00).

    Západná plocha (az 90) má špičku posunutú na 14:00, aby súčet nebol triviálny.
    """
    shift = 2 if azimuth > 0 else 0
    watts = {
        f"2026-09-21T{6 + shift:02d}:00:00+02:00": 0,
        f"2026-09-21T{12 + shift:02d}:00:00+02:00": 2000,
        f"2026-09-21T{18 + shift:02d}:00:00+02:00": 0,
        f"2026-09-22T{6 + shift:02d}:00:00+02:00": 0,
        f"2026-09-22T{12 + shift:02d}:00:00+02:00": 1000,
        f"2026-09-22T{18 + shift:02d}:00:00+02:00": 0,
    }
    return PlaneEstimate(
        watts=watts,
        fetched_at=datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
        ratelimit=RateLimit(limit=12, remaining=10, period=3600),
        place="Bratislava",
        timezone="Europe/Bratislava",
    )


async def _mock_estimate(lat, lon, declination, azimuth, kwp, **kwargs):
    return _estimate_for(azimuth)


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="FS Plus",
        data={
            CONF_NAME: "FS Plus",
            CONF_LATITUDE: 48.15,
            CONF_LONGITUDE: 17.11,
            CONF_API_KEY: "",
            CONF_PLANES: [
                {
                    CONF_PLANE_ID: "east",
                    CONF_PLANE_NAME: "Východ",
                    CONF_DECLINATION: 35,
                    CONF_AZIMUTH: -90,
                    CONF_KWP: 4,
                },
                {CONF_PLANE_ID: "west", CONF_PLANE_NAME: "Západ", CONF_DECLINATION: 35, CONF_AZIMUTH: 90, CONF_KWP: 4},
            ],
        },
        options={CONF_UPDATE_INTERVAL: 15, CONF_INVERTER_KW: 0},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncMock:
    # Testovacie HA má predvolene americké pásmo – hodnoty v testoch sú pre Bratislavu.
    await hass.config.async_set_time_zone("Europe/Bratislava")
    entry.add_to_hass(hass)
    with patch(
        "custom_components.forecast_solar_plus.coordinator.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=_mock_estimate),
    ) as mock:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return mock


async def test_setup_creates_combined_and_plane_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, frozen_now
) -> None:
    mock = await _setup(hass, entry)
    # Jedno volanie na plochu
    assert mock.await_count == 2

    today = hass.states.get("sensor.fs_plus_forecast_today")
    assert today is not None
    # Východ: trojuholník 12 h × 2000 W / 2 = 12 kWh; západ rovnako → 24 kWh
    assert float(today.state) == pytest.approx(24.0, abs=0.01)
    detailed = today.attributes[ATTR_DETAILED_FORECAST]
    assert len(detailed) == 48
    assert detailed[0]["period_start"].hour == 0
    # 12:00–12:30 lokálne: východ klesá z 2000, západ stúpa (2000·(6h−0)/6h…) → súčet ≈ 3.4 kW
    p = next(x for x in detailed if x["period_start"].hour == 12 and x["period_start"].minute == 0)
    assert p["pv_estimate"] == pytest.approx(2000 / 1000 * (1 - 0.25 / 6) + 2000 / 1000 * (4.25 / 6), abs=0.01)

    east = hass.states.get("sensor.fs_plus_vychod_forecast_today")
    assert east is not None
    assert float(east.state) == pytest.approx(12.0, abs=0.01)
    assert east.attributes["plane"] == "Východ"

    # Výkon teraz o 12:15 lokálne: východ 2000·(1−0.25/6) + západ 2000·(4.25/6)
    power = hass.states.get("sensor.fs_plus_power_now")
    assert power is not None
    assert float(power.state) == pytest.approx(2000 * (1 - 0.25 / 6) + 2000 * (4.25 / 6), abs=1)

    remaining = hass.states.get("sensor.fs_plus_forecast_remaining_today")
    assert remaining is not None
    assert 0 < float(remaining.state) < 24
    series = remaining.attributes[ATTR_DETAILED_FORECAST]
    values = [x["pv_estimate"] for x in series]
    assert all(a >= b for a, b in zip(values, values[1:], strict=False))

    peak = hass.states.get("sensor.fs_plus_peak_forecast_today")
    assert peak is not None
    assert float(peak.state) > 2000  # súčet plôch presahuje špičku jednej plochy

    tomorrow = hass.states.get("sensor.fs_plus_forecast_tomorrow")
    assert float(tomorrow.state) == pytest.approx(12.0, abs=0.01)

    api_limit = hass.states.get("sensor.fs_plus_api_limit")
    assert api_limit is not None and api_limit.state == "12"


async def test_inverter_limit_clips_combined_only(hass: HomeAssistant, frozen_now) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="FS Plus",
        data={
            CONF_NAME: "FS Plus",
            CONF_LATITUDE: 48.15,
            CONF_LONGITUDE: 17.11,
            CONF_API_KEY: "",
            CONF_PLANES: [
                {
                    CONF_PLANE_ID: "east",
                    CONF_PLANE_NAME: "Východ",
                    CONF_DECLINATION: 35,
                    CONF_AZIMUTH: -90,
                    CONF_KWP: 4,
                },
                {CONF_PLANE_ID: "west", CONF_PLANE_NAME: "Západ", CONF_DECLINATION: 35, CONF_AZIMUTH: 90, CONF_KWP: 4},
            ],
        },
        options={CONF_UPDATE_INTERVAL: 15, CONF_INVERTER_KW: 2.5},
    )
    await _setup(hass, entry)
    peak = hass.states.get("sensor.fs_plus_peak_forecast_today")
    assert float(peak.state) == pytest.approx(2500)
    # Plocha sa neoreže
    east_peak = hass.states.get("sensor.fs_plus_vychod_peak_forecast_today")
    assert float(east_peak.state) == pytest.approx(2000)


async def test_services(hass: HomeAssistant, entry: MockConfigEntry, frozen_now) -> None:
    await _setup(hass, entry)
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_QUERY_FORECAST_DATA,
        {"start_date_time": "2026-09-21 06:00:00", "end_date_time": "2026-09-21 08:00:00"},
        blocking=True,
        return_response=True,
    )
    assert len(response["data"]) == 4
    assert response["data"][0]["period_start"].startswith("2026-09-21T06:00:00")

    per_plane = await hass.services.async_call(
        DOMAIN,
        SERVICE_QUERY_FORECAST_DATA,
        {"start_date_time": "2026-09-21 06:00:00", "end_date_time": "2026-09-21 08:00:00", "plane": "západ"},
        blocking=True,
        return_response=True,
    )
    # Západ začína až o 08:00 → do 08:00 je 0
    assert all(x["pv_estimate"] == 0 for x in per_plane["data"])

    with patch(
        "custom_components.forecast_solar_plus.coordinator.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=_mock_estimate),
    ) as mock:
        await hass.services.async_call(DOMAIN, SERVICE_FORCE_UPDATE, {}, blocking=True)
        await hass.async_block_till_done()
    assert mock.await_count == 2


async def test_cache_used_after_reload(hass: HomeAssistant, entry: MockConfigEntry, frozen_now) -> None:
    """Po reloade sa v rámci intervalu API nevolá – dáta prídu z .storage."""
    await _setup(hass, entry)
    with patch(
        "custom_components.forecast_solar_plus.coordinator.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=_mock_estimate),
    ) as mock:
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert mock.await_count == 0
    polled = hass.states.get("sensor.fs_plus_api_last_polled")
    assert polled is not None
    assert polled.attributes["from_cache"] is True
    assert hass.states.get("sensor.fs_plus_forecast_today").state != "unavailable"


async def test_api_failure_keeps_last_data(hass: HomeAssistant, entry: MockConfigEntry, frozen_now) -> None:
    from custom_components.forecast_solar_plus.api import ForecastSolarRateLimitError

    await _setup(hass, entry)
    coordinator = entry.runtime_data
    with patch(
        "custom_components.forecast_solar_plus.coordinator.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=ForecastSolarRateLimitError("limit", RateLimit(limit=12, remaining=0))),
    ):
        await coordinator.async_force_refresh()
        await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert coordinator.data.from_cache is True
    assert coordinator.data.last_error == "limit"
    assert hass.states.get("sensor.fs_plus_api_remaining").state == "0"
    assert float(hass.states.get("sensor.fs_plus_forecast_today").state) == pytest.approx(24.0, abs=0.01)


async def test_config_flow_two_planes(hass: HomeAssistant) -> None:
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType

    with patch(
        "custom_components.forecast_solar_plus.config_flow.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=_mock_estimate),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert result["type"] == FlowResultType.FORM and result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Strecha", CONF_LATITUDE: 48.1, CONF_LONGITUDE: 17.1, CONF_API_KEY: ""}
        )
        assert result["step_id"] == "plane"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PLANE_NAME: "Východ", CONF_DECLINATION: 35, CONF_AZIMUTH: -90, CONF_KWP: 4, "add_another": True},
        )
        assert result["step_id"] == "plane"
        with patch("custom_components.forecast_solar_plus.async_setup_entry", return_value=True):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_PLANE_NAME: "Západ", CONF_DECLINATION: 35, CONF_AZIMUTH: 90, CONF_KWP: 4, "add_another": False},
            )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Strecha"
    assert len(result["data"][CONF_PLANES]) == 2
    # 2 plochy bez kľúča: minimum 13 min → predvolené 15
    assert result["options"][CONF_UPDATE_INTERVAL] == 15


async def test_options_add_plane_raises_interval(hass: HomeAssistant, entry: MockConfigEntry, frozen_now) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    await _setup(hass, entry)
    # Nastavíme interval tesne na minimum pre 2 plochy (13) a pridáme tretiu → musí sa zdvihnúť na 19
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_UPDATE_INTERVAL: 13})
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_plane"})
    assert result["step_id"] == "add_plane"
    with patch(
        "custom_components.forecast_solar_plus.coordinator.ForecastSolarApi.estimate",
        new=AsyncMock(side_effect=_mock_estimate),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PLANE_NAME: "Juh", CONF_DECLINATION: 30, CONF_AZIMUTH: 0, CONF_KWP: 2}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(entry.data[CONF_PLANES]) == 3
    assert entry.options[CONF_UPDATE_INTERVAL] == 19
    assert hass.states.get("sensor.fs_plus_juh_forecast_today") is not None
