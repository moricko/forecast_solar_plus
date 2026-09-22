"""Senzory predpovede – názvy a atribúty zodpovedajú Solcast PV Forecast.

Každý senzor číta hodnotu priamo z krivky výkonu v momente čítania stavu, takže
senzory viazané na „teraz" (výkon, zostávajúca výroba, táto hodina) sú aktuálne
aj medzi volaniami API – koordinátor ich každú minútu prinúti prepočítať sa.

Súhrnné senzory pracujú so zlúčenou krivkou všetkých plôch, senzory jednotlivých
plôch s krivkou danej plochy (bez orezania striedačom).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DATA_CORRECT,
    ATTR_DAY_NAME,
    ATTR_DETAILED_FORECAST,
    ATTR_DETAILED_HOURLY,
    ATTR_PERIOD_START,
    ATTR_PV_ESTIMATE,
    DOMAIN,
    FORECAST_DAYS,
    FORECAST_PERIOD,
    HOURLY_PERIOD,
)
from .coordinator import ForecastSolarData, ForecastSolarPlusConfigEntry, ForecastSolarPlusCoordinator, PlaneConfig
from .forecast import PowerCurve

_LOGGER = logging.getLogger(__name__)

# Zaokrúhľovanie výstupov: energia v kWh na 3 desatiny, priemerný výkon periódy (kW) na 4.
_KWH_DIGITS = 3
_KW_DIGITS = 4


@dataclass(frozen=True)
class TimeWindows:
    """Časové okná odvodené od aktuálneho času (všetko v UTC).

    `days[n]` – (začiatok, koniec) n-tého dňa od dnes v lokálnom čase HA,
    `hour` – aktuálna celá hodina, `next_hour` – nasledujúca celá hodina.
    """

    now: datetime
    days: list[tuple[datetime, datetime]]
    hour: tuple[datetime, datetime]
    next_hour: tuple[datetime, datetime]

    @classmethod
    def build(cls) -> TimeWindows:
        """Vypočíta okná z aktuálneho lokálneho času (rešpektuje zmenu letného času)."""
        local_now = dt_util.now()
        day_starts = [dt_util.start_of_local_day(local_now + timedelta(days=n)) for n in range(FORECAST_DAYS + 1)]
        days = [(dt_util.as_utc(day_starts[n]), dt_util.as_utc(day_starts[n + 1])) for n in range(FORECAST_DAYS)]
        hour_start = dt_util.as_utc(local_now.replace(minute=0, second=0, microsecond=0))
        return cls(
            now=dt_util.as_utc(local_now),
            days=days,
            hour=(hour_start, hour_start + timedelta(hours=1)),
            next_hour=(hour_start + timedelta(hours=1), hour_start + timedelta(hours=2)),
        )


# ----------------------------------------------------------------- pomocné výpočty


def _kwh(value_wh: float) -> float:
    """Wh → kWh so zaokrúhlením pre stav senzora."""
    return round(value_wh / 1000.0, _KWH_DIGITS)


def _day_energy(curve: PowerCurve, tw: TimeWindows, day: int) -> float | None:
    """Energia (kWh) za n-tý deň; None, ak predpoveď na ten deň nesiaha."""
    start, end = tw.days[day]
    if curve.end is None or curve.end <= start:
        return None
    return _kwh(curve.energy_wh(start, end))


def _detailed_attrs(curve: PowerCurve, start: datetime, end: datetime) -> dict[str, Any]:
    """Atribúty `detailedForecast` (30 min) a `detailedHourly` (60 min) – priemerný výkon v kW."""
    return {
        ATTR_DETAILED_FORECAST: [
            {ATTR_PERIOD_START: dt_util.as_local(p.start), ATTR_PV_ESTIMATE: round(p.value / 1000.0, _KW_DIGITS)}
            for p in curve.periods(start, end, FORECAST_PERIOD)
        ],
        ATTR_DETAILED_HOURLY: [
            {ATTR_PERIOD_START: dt_util.as_local(p.start), ATTR_PV_ESTIMATE: round(p.value / 1000.0, _KW_DIGITS)}
            for p in curve.periods(start, end, HOURLY_PERIOD)
        ],
    }


def _day_attrs(day: int) -> Callable[[PowerCurve, TimeWindows], dict[str, Any]]:
    """Vytvorí funkciu atribútov pre senzor n-tého dňa (názov dňa + detailný priebeh)."""

    def _attrs(curve: PowerCurve, tw: TimeWindows) -> dict[str, Any]:
        start, end = tw.days[day]
        return {
            ATTR_DAY_NAME: dt_util.as_local(start).strftime("%A"),
            ATTR_DATA_CORRECT: curve.end is not None and curve.end > start,
            **_detailed_attrs(curve, start, end),
        }

    return _attrs


def _remaining_attrs(curve: PowerCurve, tw: TimeWindows) -> dict[str, Any]:
    """Zostávajúca výroba v čase: pre každú periódu energia (kWh), ktorá ostáva do konca dňa.

    Perióda obsahujúca „teraz" začína aktuálnym časom, aby prvý bod sedel so stavom senzora.
    """
    start, end = tw.days[0]

    def _series(step: timedelta) -> list[dict[str, Any]]:
        series = curve.remaining_series(start, end, step)
        result = []
        for period in series:
            period_start = period.start
            value = period.value
            # Bežiacu periódu nahradíme aktuálnym časom, aby graf sedel so stavom senzora.
            # Minulé periódy ostávajú pre kontext – ukazujú, koľko v tom čase ešte ostávalo.
            if period_start < tw.now < period_start + step:
                period_start, value = tw.now, curve.energy_wh(tw.now, end)
            result.append({ATTR_PERIOD_START: dt_util.as_local(period_start), ATTR_PV_ESTIMATE: _kwh(value)})
        return result

    return {ATTR_DETAILED_FORECAST: _series(FORECAST_PERIOD), ATTR_DETAILED_HOURLY: _series(HOURLY_PERIOD)}


def _peak_watts(day: int) -> Callable[[PowerCurve, TimeWindows, ForecastSolarData], StateType]:
    """Špičkový výkon (W) n-tého dňa."""

    def _value(curve: PowerCurve, tw: TimeWindows, _data: ForecastSolarData) -> StateType:
        peak = curve.peak(*tw.days[day])
        return round(peak.watts) if peak else None

    return _value


def _peak_time(day: int) -> Callable[[PowerCurve, TimeWindows, ForecastSolarData], datetime | None]:
    """Čas špičkového výkonu n-tého dňa."""

    def _value(curve: PowerCurve, tw: TimeWindows, _data: ForecastSolarData) -> datetime | None:
        peak = curve.peak(*tw.days[day])
        return peak.time if peak else None

    return _value


# ----------------------------------------------------------------- popisy senzorov


@dataclass(frozen=True, kw_only=True)
class ForecastSensorDescription(SensorEntityDescription):
    """Popis senzora: `value_fn` počíta stav, `attr_fn` atribúty, `per_plane` = aj pre každú plochu."""

    value_fn: Callable[[PowerCurve, TimeWindows, ForecastSolarData], StateType | datetime | None]
    attr_fn: Callable[[PowerCurve, TimeWindows], dict[str, Any]] | None = None
    per_plane: bool = False


_ENERGY = {
    "device_class": SensorDeviceClass.ENERGY,
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "suggested_display_precision": 2,
}
_POWER = {
    "device_class": SensorDeviceClass.POWER,
    "native_unit_of_measurement": UnitOfPower.WATT,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 0,
}

SENSORS: tuple[ForecastSensorDescription, ...] = (
    ForecastSensorDescription(
        key="forecast_today",
        translation_key="forecast_today",
        per_plane=True,
        value_fn=lambda c, tw, _d: _day_energy(c, tw, 0),
        attr_fn=_day_attrs(0),
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_tomorrow",
        translation_key="forecast_tomorrow",
        per_plane=True,
        value_fn=lambda c, tw, _d: _day_energy(c, tw, 1),
        attr_fn=_day_attrs(1),
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_day_3",
        translation_key="forecast_day_3",
        value_fn=lambda c, tw, _d: _day_energy(c, tw, 2),
        attr_fn=_day_attrs(2),
        entity_registry_enabled_default=False,
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_day_4",
        translation_key="forecast_day_4",
        value_fn=lambda c, tw, _d: _day_energy(c, tw, 3),
        attr_fn=_day_attrs(3),
        entity_registry_enabled_default=False,
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_remaining_today",
        translation_key="forecast_remaining_today",
        per_plane=True,
        value_fn=lambda c, tw, _d: _kwh(c.energy_wh(tw.now, tw.days[0][1])),
        attr_fn=_remaining_attrs,
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_this_hour",
        translation_key="forecast_this_hour",
        value_fn=lambda c, tw, _d: _kwh(c.energy_wh(*tw.hour)),
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="forecast_next_hour",
        translation_key="forecast_next_hour",
        value_fn=lambda c, tw, _d: _kwh(c.energy_wh(*tw.next_hour)),
        **_ENERGY,
    ),
    ForecastSensorDescription(
        key="power_now",
        translation_key="power_now",
        per_plane=True,
        value_fn=lambda c, tw, _d: round(c.power_at(tw.now)),
        **_POWER,
    ),
    ForecastSensorDescription(
        key="power_now_30m",
        translation_key="power_now_30m",
        value_fn=lambda c, tw, _d: round(c.power_at(tw.now + timedelta(minutes=30))),
        **_POWER,
    ),
    ForecastSensorDescription(
        key="power_now_1h",
        translation_key="power_now_1h",
        value_fn=lambda c, tw, _d: round(c.power_at(tw.now + timedelta(hours=1))),
        **_POWER,
    ),
    ForecastSensorDescription(
        key="peak_forecast_today",
        translation_key="peak_forecast_today",
        per_plane=True,
        value_fn=_peak_watts(0),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    ForecastSensorDescription(
        key="peak_time_today",
        translation_key="peak_time_today",
        per_plane=True,
        value_fn=_peak_time(0),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    ForecastSensorDescription(
        key="peak_forecast_tomorrow",
        translation_key="peak_forecast_tomorrow",
        value_fn=_peak_watts(1),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
    ),
    ForecastSensorDescription(
        key="peak_time_tomorrow",
        translation_key="peak_time_tomorrow",
        value_fn=_peak_time(1),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    # --- diagnostika API ---
    ForecastSensorDescription(
        key="api_last_polled",
        translation_key="api_last_polled",
        value_fn=lambda _c, _tw, d: d.polled_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ForecastSensorDescription(
        key="api_limit",
        translation_key="api_limit",
        value_fn=lambda _c, _tw, d: d.ratelimit.limit if d.ratelimit else None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ForecastSensorDescription(
        key="api_used",
        translation_key="api_used",
        value_fn=lambda _c, _tw, d: d.ratelimit.used if d.ratelimit else None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ForecastSensorDescription(
        key="api_remaining",
        translation_key="api_remaining",
        value_fn=lambda _c, _tw, d: d.ratelimit.remaining if d.ratelimit else None,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# ----------------------------------------------------------------- platforma


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ForecastSolarPlusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Vytvorí súhrnné senzory a senzory jednotlivých plôch."""
    coordinator = entry.runtime_data
    entities: list[ForecastSolarPlusSensor] = [
        ForecastSolarPlusSensor(coordinator, description, plane=None) for description in SENSORS
    ]
    for plane in coordinator.planes:
        entities.extend(
            ForecastSolarPlusSensor(coordinator, description, plane=plane)
            for description in SENSORS
            if description.per_plane
        )
    _LOGGER.debug("Registrujem %d senzorov pre záznam %s", len(entities), entry.title)
    async_add_entities(entities)


class ForecastSolarPlusSensor(CoordinatorEntity[ForecastSolarPlusCoordinator], SensorEntity):
    """Jeden senzor predpovede – súhrnný alebo pre konkrétnu plochu."""

    entity_description: ForecastSensorDescription
    _attr_has_entity_name = True
    _attr_attribution = "Data provided by Forecast.Solar"

    def __init__(
        self,
        coordinator: ForecastSolarPlusCoordinator,
        description: ForecastSensorDescription,
        plane: PlaneConfig | None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._plane = plane
        entry = coordinator.config_entry
        if plane is None:
            self._attr_unique_id = f"{entry.entry_id}_{description.key}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry.entry_id)},
                name=entry.title,
                manufacturer="Forecast.Solar",
                model="Forecast.Solar Plus",
                entry_type=DeviceEntryType.SERVICE,
                configuration_url="https://forecast.solar",
            )
        else:
            self._attr_unique_id = f"{entry.entry_id}_{plane.id}_{description.key}"
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry.entry_id}_{plane.id}")},
                name=f"{entry.title} {plane.name}",
                manufacturer="Forecast.Solar",
                model=f"Plocha {plane.declination:g}° / {plane.azimuth:g}° / {plane.kwp:g} kWp",
                entry_type=DeviceEntryType.SERVICE,
                via_device=(DOMAIN, entry.entry_id),
            )

    @property
    def _curve(self) -> PowerCurve:
        """Krivka, s ktorou senzor pracuje (zlúčená alebo krivka plochy)."""
        data = self.coordinator.data
        if self._plane is None:
            return data.combined
        return data.planes.get(self._plane.id, PowerCurve())

    @property
    def native_value(self) -> StateType | datetime | None:
        """Stav senzora vypočítaný z aktuálnej krivky a aktuálneho času."""
        try:
            return self.entity_description.value_fn(self._curve, TimeWindows.build(), self.coordinator.data)
        except (ValueError, TypeError, IndexError) as err:
            _LOGGER.error("Chyba výpočtu senzora %s: %s", self.entity_id, err, exc_info=True)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Atribúty (detailný priebeh); pre plochy navyše parametre plochy."""
        attrs: dict[str, Any] = {}
        if self.entity_description.attr_fn is not None:
            try:
                attrs.update(self.entity_description.attr_fn(self._curve, TimeWindows.build()))
            except (ValueError, TypeError, IndexError) as err:
                _LOGGER.error("Chyba výpočtu atribútov senzora %s: %s", self.entity_id, err, exc_info=True)
        if self._plane is not None:
            attrs.update(
                {
                    "plane": self._plane.name,
                    "declination": self._plane.declination,
                    "azimuth": self._plane.azimuth,
                    "kwp": self._plane.kwp,
                }
            )
        if self.entity_description.key == "api_last_polled":
            data = self.coordinator.data
            attrs.update({"from_cache": data.from_cache, "last_error": data.last_error, "place": data.place})
        return attrs or None
