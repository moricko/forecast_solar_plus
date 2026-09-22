"""Koordinátor údajov: sťahuje predpovede pre všetky plochy a zlučuje ich.

Dve úrovne aktualizácie:

1. **Volanie API** každých `update_interval` minút – pre každú plochu jedno
   volanie, výsledok sa uloží do `.storage`, aby sa po reštarte Home Assistant
   nemíňal limit volaní zbytočne.
2. **Lokálny tik** každú minútu – bez volania API sa len prepočítajú senzory
   viazané na aktuálny čas (výkon teraz, zostávajúca výroba, táto hodina…).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ForecastSolarApi,
    ForecastSolarApiError,
    ForecastSolarAuthError,
    ForecastSolarRateLimitError,
    RateLimit,
)
from .const import (
    API_PLANE_DELAY_SECONDS,
    CONF_API_KEY,
    CONF_AZIMUTH,
    CONF_DAMPING_EVENING,
    CONF_DAMPING_MORNING,
    CONF_DECLINATION,
    CONF_INVERTER_KW,
    CONF_KWP,
    CONF_PLANE_ID,
    CONF_PLANE_NAME,
    CONF_PLANES,
    CONF_RESOLUTION,
    CONF_UPDATE_INTERVAL,
    DEFAULT_DAMPING,
    DEFAULT_INVERTER_KW,
    DEFAULT_RESOLUTION,
    DEFAULT_UPDATE_INTERVAL_KEY,
    DEFAULT_UPDATE_INTERVAL_PUBLIC,
    DOMAIN,
    LOCAL_TICK_SECONDS,
    STORAGE_VERSION,
)
from .forecast import PowerCurve, combine_curves

_LOGGER = logging.getLogger(__name__)

type ForecastSolarPlusConfigEntry = ConfigEntry[ForecastSolarPlusCoordinator]


@dataclass(frozen=True)
class PlaneConfig:
    """Konfigurácia jednej plochy (orientácie) fotovoltiky."""

    id: str
    name: str
    declination: float
    azimuth: float
    kwp: float

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaneConfig:
        """Vytvorí konfiguráciu z uloženého slovníka config entry."""
        return cls(
            id=str(data[CONF_PLANE_ID]),
            name=str(data[CONF_PLANE_NAME]),
            declination=float(data[CONF_DECLINATION]),
            azimuth=float(data[CONF_AZIMUTH]),
            kwp=float(data[CONF_KWP]),
        )


@dataclass(frozen=True)
class _CachedPlanes:
    """Posledné úspešne stiahnuté krivky – zdroj pre senzory aj pre úložisko."""

    polled_at: datetime
    curves: dict[str, PowerCurve]
    ratelimit: RateLimit | None
    place: str | None

    def to_storage(self) -> dict[str, Any]:
        """Tvar pre JSON úložisko."""
        return {
            "polled_at": self.polled_at.isoformat(),
            "place": self.place,
            "ratelimit": self.ratelimit.as_dict() if self.ratelimit else None,
            "planes": {plane_id: curve.serialize() for plane_id, curve in self.curves.items()},
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> _CachedPlanes:
        """Obnova z úložiska; pri poškodených dátach vyhodí ValueError/KeyError."""
        return cls(
            polled_at=datetime.fromisoformat(data["polled_at"]),
            curves={str(k): PowerCurve.from_serialized(v) for k, v in (data.get("planes") or {}).items()},
            ratelimit=RateLimit.from_dict(data.get("ratelimit")),
            place=data.get("place"),
        )


@dataclass(frozen=True)
class ForecastSolarData:
    """Údaje, ktoré koordinátor poskytuje entitám.

    `planes` – krivka každej plochy podľa jej id, `combined` – súčet všetkých
    plôch orezaný limitom striedača, `from_cache` – True, ak sa pri tejto
    aktualizácii API nevolalo (medzipamäť alebo zlyhanie s ponechaním starých dát).
    """

    polled_at: datetime
    planes: dict[str, PowerCurve]
    combined: PowerCurve
    ratelimit: RateLimit | None
    place: str | None
    from_cache: bool
    last_error: str | None = None


class ForecastSolarPlusCoordinator(DataUpdateCoordinator[ForecastSolarData]):
    """Sťahuje a zlučuje predpovede Forecast.Solar pre všetky plochy jedného záznamu."""

    config_entry: ForecastSolarPlusConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ForecastSolarPlusConfigEntry) -> None:
        self.planes: list[PlaneConfig] = [PlaneConfig.from_dict(p) for p in entry.data.get(CONF_PLANES, [])]
        self.api = ForecastSolarApi(async_get_clientsession(hass), entry.data.get(CONF_API_KEY))
        self.latitude = float(entry.data[CONF_LATITUDE])
        self.longitude = float(entry.data[CONF_LONGITUDE])

        options = entry.options
        default_interval = DEFAULT_UPDATE_INTERVAL_KEY if self.api.has_api_key else DEFAULT_UPDATE_INTERVAL_PUBLIC
        self.update_interval_minutes = int(options.get(CONF_UPDATE_INTERVAL, default_interval))
        self.damping_morning = float(options.get(CONF_DAMPING_MORNING, DEFAULT_DAMPING))
        self.damping_evening = float(options.get(CONF_DAMPING_EVENING, DEFAULT_DAMPING))
        self.inverter_kw = float(options.get(CONF_INVERTER_KW, DEFAULT_INVERTER_KW))
        self.resolution = str(options.get(CONF_RESOLUTION, DEFAULT_RESOLUTION) or "")

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=self.update_interval_minutes),
        )

        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._cache: _CachedPlanes | None = None
        self._force_next = False
        self._unsub_tick: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------ životný cyklus

    async def async_load_cache(self) -> None:
        """Načíta poslednú predpoveď z úložiska (ak sedí zoznam plôch)."""
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 – úložisko nesmie zhodiť štart integrácie
            _LOGGER.warning("Nepodarilo sa načítať medzipamäť predpovede: %s", err)
            return
        if not stored:
            _LOGGER.debug("Medzipamäť predpovede je prázdna")
            return
        try:
            cache = _CachedPlanes.from_storage(stored)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning("Medzipamäť predpovede je poškodená, ignorujem ju: %s", err)
            return
        expected = {plane.id for plane in self.planes}
        if set(cache.curves) != expected:
            _LOGGER.info("Zoznam plôch sa zmenil, medzipamäť predpovede sa zahodí")
            return
        self._cache = cache
        _LOGGER.info(
            "Načítaná medzipamäť predpovede z %s (%d plôch)",
            dt_util.as_local(cache.polled_at).isoformat(timespec="minutes"),
            len(cache.curves),
        )

    @callback
    def async_start_local_tick(self) -> None:
        """Spustí minútový prepočet senzorov viazaných na aktuálny čas."""
        if self._unsub_tick is None:
            self._unsub_tick = async_track_time_interval(
                self.hass, self._handle_local_tick, timedelta(seconds=LOCAL_TICK_SECONDS)
            )

    async def async_shutdown(self) -> None:
        """Zruší lokálny tik a ukončí koordinátor."""
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        await super().async_shutdown()

    @callback
    def _handle_local_tick(self, _now: datetime) -> None:
        """Bez volania API prinúti entity znovu prečítať hodnoty z aktuálnych kriviek."""
        if self.data is not None:
            self.async_update_listeners()

    async def async_force_refresh(self) -> None:
        """Vynúti volanie API bez ohľadu na medzipamäť (služba `force_update_forecasts`)."""
        self._force_next = True
        await self.async_refresh()

    # ---------------------------------------------------------------- aktualizácia

    async def _async_update_data(self) -> ForecastSolarData:
        """Stiahne predpovede všetkých plôch; pri zlyhaní ponechá posledné známe dáta."""
        now = dt_util.utcnow()
        if (
            not self._force_next
            and self._cache is not None
            and now - self._cache.polled_at < timedelta(minutes=self.update_interval_minutes)
        ):
            _LOGGER.debug("Používam medzipamäť predpovede z %s", self._cache.polled_at.isoformat())
            return self._build_data(self._cache, from_cache=True)
        self._force_next = False

        if not self.planes:
            raise UpdateFailed("Nie je nakonfigurovaná žiadna plocha")

        curves: dict[str, PowerCurve] = {}
        ratelimit: RateLimit | None = None
        place: str | None = None
        for index, plane in enumerate(self.planes):
            if index:
                await asyncio.sleep(API_PLANE_DELAY_SECONDS)
            try:
                estimate = await self.api.estimate(
                    self.latitude,
                    self.longitude,
                    plane.declination,
                    plane.azimuth,
                    plane.kwp,
                    damping_morning=self.damping_morning,
                    damping_evening=self.damping_evening,
                    resolution=self.resolution or None,
                )
            except ForecastSolarAuthError as err:
                _LOGGER.error("Neplatný API kľúč Forecast.Solar: %s", err)
                raise ConfigEntryAuthFailed(str(err)) from err
            except ForecastSolarRateLimitError as err:
                _LOGGER.warning("Plocha „%s“: %s – ponechávam posledné známe dáta", plane.name, err)
                return self._fallback(str(err), err.ratelimit)
            except ForecastSolarApiError as err:
                _LOGGER.error("Plocha „%s“: %s", plane.name, err)
                return self._fallback(str(err), None)

            curve = PowerCurve.from_api_watts(estimate.watts)
            if curve.is_empty:
                _LOGGER.warning("Plocha „%s“: API vrátilo prázdnu predpoveď", plane.name)
            curves[plane.id] = curve
            ratelimit = estimate.ratelimit or ratelimit
            place = estimate.place or place
            _LOGGER.debug("Plocha „%s“: %r", plane.name, curve)

        self._cache = _CachedPlanes(polled_at=now, curves=curves, ratelimit=ratelimit, place=place)
        await self._async_save_cache()
        _LOGGER.info(
            "Predpoveď aktualizovaná (%d plôch), limit API: %s/%s",
            len(curves),
            ratelimit.remaining if ratelimit else "?",
            ratelimit.limit if ratelimit else "?",
        )
        return self._build_data(self._cache, from_cache=False)

    def _fallback(self, error: str, ratelimit: RateLimit | None) -> ForecastSolarData:
        """Po chybe API vráti posledné známe dáta; bez nich nahlási zlyhanie aktualizácie."""
        if self._cache is None:
            raise UpdateFailed(error)
        cache = self._cache
        if ratelimit is not None:
            # Aktualizujeme aspoň stav limitu, aby senzory api_* ukazovali skutočnosť.
            cache = _CachedPlanes(cache.polled_at, cache.curves, ratelimit, cache.place)
            self._cache = cache
        return self._build_data(cache, from_cache=True, last_error=error)

    def _build_data(
        self, cache: _CachedPlanes, *, from_cache: bool, last_error: str | None = None
    ) -> ForecastSolarData:
        """Zloží výstup pre entity: zlúči plochy a aplikuje limit striedača."""
        combined = combine_curves(cache.curves.values(), inverter_watts=self.inverter_kw * 1000.0)
        return ForecastSolarData(
            polled_at=cache.polled_at,
            planes=dict(cache.curves),
            combined=combined,
            ratelimit=cache.ratelimit,
            place=cache.place,
            from_cache=from_cache,
            last_error=last_error,
        )

    async def _async_save_cache(self) -> None:
        """Uloží medzipamäť; zlyhanie zápisu len zaloguje (dáta v pamäti ostávajú)."""
        if self._cache is None:
            return
        try:
            await self._store.async_save(self._cache.to_storage())
        except Exception as err:  # noqa: BLE001 – zápis na disk nesmie zhodiť aktualizáciu
            _LOGGER.warning("Nepodarilo sa uložiť medzipamäť predpovede: %s", err)

    # ---------------------------------------------------------------- pomocné

    def plane_by_name(self, name: str) -> PlaneConfig | None:
        """Nájde plochu podľa názvu (bez ohľadu na veľkosť písmen)."""
        wanted = name.strip().casefold()
        return next((p for p in self.planes if p.name.casefold() == wanted or p.id == name), None)
