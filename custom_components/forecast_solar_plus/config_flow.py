"""Konfiguračný tok integrácie – nastavenie cez UI.

Kroky pri pridaní:
1. `user`    – názov, poloha (predvolene z nastavení HA) a voliteľný API kľúč,
2. `plane`   – parametre jednej plochy; zaškrtnutím „pridať ďalšiu" sa krok opakuje,
3. overovacie volanie API pre prvú plochu a vytvorenie záznamu.

Možnosti (ozubené koliesko) ponúkajú menu: nastavenia (interval, tlmenie, striedač,
rozlíšenie), pridanie plochy a odstránenie plochy. Zmena plôch sa zapisuje do
`entry.data` a integrácia sa znovu načíta.
"""

from __future__ import annotations

import logging
from math import ceil
from typing import Any
import uuid

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ForecastSolarApi, ForecastSolarApiError, ForecastSolarAuthError, ForecastSolarRateLimitError
from .const import (
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
    MAX_UPDATE_INTERVAL,
    RATE_LIMIT_PERSONAL,
    RATE_LIMIT_PUBLIC,
    RATE_LIMIT_SAFETY_MARGIN,
    RESOLUTION_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)

# Pomocný kľúč formulára plochy – nie je súčasťou uložených dát.
_FORM_ADD_ANOTHER = "add_another"
# Kľúč formulára pre výber plôch na odstránenie.
_FORM_REMOVE = "remove"


def min_update_interval(plane_count: int, has_api_key: bool) -> int:
    """Najkratší bezpečný interval (min) tak, aby sa nevyčerpal kĺzavý hodinový limit API.

    Každá plocha je jedno volanie; z limitu využívame len `RATE_LIMIT_SAFETY_MARGIN`,
    aby ostala rezerva na ručné obnovenie a reštarty.
    """
    limit = RATE_LIMIT_PERSONAL if has_api_key else RATE_LIMIT_PUBLIC
    usable_per_hour = max(limit * RATE_LIMIT_SAFETY_MARGIN, 1.0)
    return max(1, ceil(60.0 * max(plane_count, 1) / usable_per_hour))


def default_update_interval(plane_count: int, has_api_key: bool) -> int:
    """Predvolený interval: plánová predvoľba, no nikdy kratší než bezpečné minimum."""
    default = DEFAULT_UPDATE_INTERVAL_KEY if has_api_key else DEFAULT_UPDATE_INTERVAL_PUBLIC
    return max(default, min_update_interval(plane_count, has_api_key))


def _plane_schema(defaults: dict[str, Any] | None = None, *, with_add_another: bool) -> vol.Schema:
    """Formulár jednej plochy; `with_add_another` pridá prepínač na opakovanie kroku."""
    defaults = defaults or {}
    fields: dict[Any, Any] = {
        vol.Required(CONF_PLANE_NAME, default=defaults.get(CONF_PLANE_NAME, "")): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(CONF_DECLINATION, default=defaults.get(CONF_DECLINATION, 30)): NumberSelector(
            NumberSelectorConfig(min=0, max=90, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°")
        ),
        vol.Required(CONF_AZIMUTH, default=defaults.get(CONF_AZIMUTH, 0)): NumberSelector(
            NumberSelectorConfig(min=-180, max=180, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="°")
        ),
        vol.Required(CONF_KWP, default=defaults.get(CONF_KWP, 5.0)): NumberSelector(
            NumberSelectorConfig(min=0.01, max=1000, step=0.01, mode=NumberSelectorMode.BOX, unit_of_measurement="kWp")
        ),
    }
    if with_add_another:
        fields[vol.Optional(_FORM_ADD_ANOTHER, default=False)] = BooleanSelector()
    return vol.Schema(fields)


def _plane_from_form(user_input: dict[str, Any]) -> dict[str, Any]:
    """Prevedie hodnoty formulára na uložený záznam plochy s vygenerovaným id."""
    return {
        CONF_PLANE_ID: uuid.uuid4().hex[:8],
        CONF_PLANE_NAME: str(user_input[CONF_PLANE_NAME]).strip(),
        CONF_DECLINATION: float(user_input[CONF_DECLINATION]),
        CONF_AZIMUTH: float(user_input[CONF_AZIMUTH]),
        CONF_KWP: float(user_input[CONF_KWP]),
    }


def _plane_label(plane: dict[str, Any]) -> str:
    """Popis plochy pre výberové zoznamy: názov (sklon / azimut / kWp)."""
    return (
        f"{plane[CONF_PLANE_NAME]} ({plane[CONF_DECLINATION]:g}° / {plane[CONF_AZIMUTH]:g}° / {plane[CONF_KWP]:g} kWp)"
    )


async def _validate_api(
    hass: HomeAssistant, api_key: str | None, latitude: float, longitude: float, plane: dict[str, Any]
) -> str | None:
    """Skúšobné volanie API pre jednu plochu; vráti kód chyby pre formulár alebo None."""
    api = ForecastSolarApi(async_get_clientsession(hass), api_key)
    try:
        await api.estimate(latitude, longitude, plane[CONF_DECLINATION], plane[CONF_AZIMUTH], plane[CONF_KWP])
    except ForecastSolarAuthError as err:
        _LOGGER.warning("Overenie API kľúča zlyhalo: %s", err)
        return "invalid_auth"
    except ForecastSolarRateLimitError as err:
        _LOGGER.warning("Overenie zlyhalo pre limit volaní: %s", err)
        return "rate_limited"
    except ForecastSolarApiError as err:
        _LOGGER.warning("Overenie spojenia s Forecast.Solar zlyhalo: %s", err)
        return "cannot_connect"
    return None


class ForecastSolarPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sprievodca pridaním integrácie."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._planes: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ForecastSolarPlusOptionsFlow:
        """Vráti tok možností pre existujúci záznam."""
        return ForecastSolarPlusOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Krok 1: názov, poloha, voliteľný API kľúč."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_NAME: str(user_input[CONF_NAME]).strip() or "Forecast.Solar Plus",
                CONF_LATITUDE: float(user_input[CONF_LATITUDE]),
                CONF_LONGITUDE: float(user_input[CONF_LONGITUDE]),
                CONF_API_KEY: str(user_input.get(CONF_API_KEY, "")).strip(),
            }
            return await self.async_step_plane()

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Forecast.Solar Plus"): TextSelector(),
                vol.Required(CONF_LATITUDE, default=self.hass.config.latitude): NumberSelector(
                    NumberSelectorConfig(min=-90, max=90, step="any", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_LONGITUDE, default=self.hass.config.longitude): NumberSelector(
                    NumberSelectorConfig(min=-180, max=180, step="any", mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_API_KEY, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Krok 2 (opakovateľný): parametre jednej plochy."""
        errors: dict[str, str] = {}
        if user_input is not None:
            plane = _plane_from_form(user_input)
            if not plane[CONF_PLANE_NAME]:
                errors[CONF_PLANE_NAME] = "name_required"
            elif any(p[CONF_PLANE_NAME].casefold() == plane[CONF_PLANE_NAME].casefold() for p in self._planes):
                errors[CONF_PLANE_NAME] = "name_exists"
            else:
                self._planes.append(plane)
                if user_input.get(_FORM_ADD_ANOTHER):
                    return await self.async_step_plane()
                return await self._async_finish()

        schema = _plane_schema({CONF_PLANE_NAME: f"Plocha {len(self._planes) + 1}"}, with_add_another=True)
        return self.async_show_form(
            step_id="plane",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(self._planes))},
        )

    async def _async_finish(self) -> ConfigFlowResult:
        """Overí API na prvej ploche a vytvorí záznam s predvolenými možnosťami."""
        api_key = self._data.get(CONF_API_KEY) or None
        error = await _validate_api(
            self.hass, api_key, self._data[CONF_LATITUDE], self._data[CONF_LONGITUDE], self._planes[0]
        )
        if error:
            # Vrátime používateľa na krok plochy s chybou; už zadané plochy ostávajú.
            last = self._planes.pop()
            return self.async_show_form(
                step_id="plane",
                data_schema=_plane_schema(last, with_add_another=True),
                errors={"base": error},
                description_placeholders={"count": str(len(self._planes))},
            )

        interval = default_update_interval(len(self._planes), api_key is not None)
        _LOGGER.info(
            "Vytváram záznam „%s“ s %d plochami, interval %d min", self._data[CONF_NAME], len(self._planes), interval
        )
        return self.async_create_entry(
            title=self._data[CONF_NAME],
            data={**self._data, CONF_PLANES: self._planes},
            options={
                CONF_UPDATE_INTERVAL: interval,
                CONF_DAMPING_MORNING: DEFAULT_DAMPING,
                CONF_DAMPING_EVENING: DEFAULT_DAMPING,
                CONF_INVERTER_KW: DEFAULT_INVERTER_KW,
                CONF_RESOLUTION: DEFAULT_RESOLUTION,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Spustené po `ConfigEntryAuthFailed` – vyžiada nový API kľúč."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Formulár nového API kľúča; overí ho na prvej ploche záznamu."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = str(user_input.get(CONF_API_KEY, "")).strip() or None
            planes = entry.data.get(CONF_PLANES) or []
            error = (
                await _validate_api(
                    self.hass, api_key, entry.data[CONF_LATITUDE], entry.data[CONF_LONGITUDE], planes[0]
                )
                if planes
                else None
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: api_key or ""})
        schema = vol.Schema(
            {vol.Optional(CONF_API_KEY, default=""): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)


class ForecastSolarPlusOptionsFlow(OptionsFlow):
    """Možnosti: nastavenia, pridanie a odstránenie plochy."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu možností."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "add_plane", "remove_plane"])

    @property
    def _has_api_key(self) -> bool:
        """True, ak záznam používa API kľúč (ovplyvňuje minimálny interval a rozlíšenie)."""
        return bool(self.config_entry.data.get(CONF_API_KEY))

    @property
    def _planes(self) -> list[dict[str, Any]]:
        """Aktuálny zoznam plôch záznamu."""
        return list(self.config_entry.data.get(CONF_PLANES) or [])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Interval aktualizácie, tlmenie, limit striedača, rozlíšenie."""
        options = self.config_entry.options
        minimum = min_update_interval(len(self._planes), self._has_api_key)
        errors: dict[str, str] = {}

        if user_input is not None:
            interval = int(user_input[CONF_UPDATE_INTERVAL])
            if interval < minimum:
                errors[CONF_UPDATE_INTERVAL] = "interval_too_short"
            else:
                new_options = {
                    CONF_UPDATE_INTERVAL: interval,
                    CONF_DAMPING_MORNING: float(user_input.get(CONF_DAMPING_MORNING, DEFAULT_DAMPING)),
                    CONF_DAMPING_EVENING: float(user_input.get(CONF_DAMPING_EVENING, DEFAULT_DAMPING)),
                    CONF_INVERTER_KW: float(user_input.get(CONF_INVERTER_KW, DEFAULT_INVERTER_KW)),
                    CONF_RESOLUTION: str(user_input.get(CONF_RESOLUTION, DEFAULT_RESOLUTION) or ""),
                }
                _LOGGER.info("Nové možnosti záznamu „%s“: %s", self.config_entry.title, new_options)
                return self.async_create_entry(data=new_options)

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=options.get(
                    CONF_UPDATE_INTERVAL, default_update_interval(len(self._planes), self._has_api_key)
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1, max=MAX_UPDATE_INTERVAL, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                )
            ),
            vol.Optional(
                CONF_DAMPING_MORNING, default=options.get(CONF_DAMPING_MORNING, DEFAULT_DAMPING)
            ): NumberSelector(NumberSelectorConfig(min=-0.25, max=5, step=0.01, mode=NumberSelectorMode.BOX)),
            vol.Optional(
                CONF_DAMPING_EVENING, default=options.get(CONF_DAMPING_EVENING, DEFAULT_DAMPING)
            ): NumberSelector(NumberSelectorConfig(min=-0.25, max=5, step=0.01, mode=NumberSelectorMode.BOX)),
            vol.Optional(CONF_INVERTER_KW, default=options.get(CONF_INVERTER_KW, DEFAULT_INVERTER_KW)): NumberSelector(
                NumberSelectorConfig(min=0, max=1000, step=0.01, mode=NumberSelectorMode.BOX, unit_of_measurement="kW")
            ),
        }
        if self._has_api_key:
            fields[vol.Optional(CONF_RESOLUTION, default=options.get(CONF_RESOLUTION, DEFAULT_RESOLUTION))] = (
                SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=value, label=value or "auto") for value in RESOLUTION_OPTIONS],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="resolution",
                    )
                )
            )
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"minimum": str(minimum)},
        )

    async def async_step_add_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pridá plochu do `entry.data` a znovu načíta integráciu."""
        errors: dict[str, str] = {}
        planes = self._planes
        if user_input is not None:
            plane = _plane_from_form(user_input)
            if not plane[CONF_PLANE_NAME]:
                errors[CONF_PLANE_NAME] = "name_required"
            elif any(p[CONF_PLANE_NAME].casefold() == plane[CONF_PLANE_NAME].casefold() for p in planes):
                errors[CONF_PLANE_NAME] = "name_exists"
            else:
                planes.append(plane)
                return self._async_save_planes(planes)
        return self.async_show_form(
            step_id="add_plane",
            data_schema=_plane_schema({CONF_PLANE_NAME: f"Plocha {len(planes) + 1}"}, with_add_another=False),
            errors=errors,
        )

    async def async_step_remove_plane(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Odstráni vybrané plochy; aspoň jedna musí ostať."""
        errors: dict[str, str] = {}
        planes = self._planes
        if user_input is not None:
            remove = set(user_input.get(_FORM_REMOVE) or [])
            remaining = [p for p in planes if p[CONF_PLANE_ID] not in remove]
            if not remove:
                errors[_FORM_REMOVE] = "nothing_selected"
            elif not remaining:
                errors[_FORM_REMOVE] = "last_plane"
            else:
                return self._async_save_planes(remaining)
        schema = vol.Schema(
            {
                vol.Required(_FORM_REMOVE, default=[]): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=p[CONF_PLANE_ID], label=_plane_label(p)) for p in planes],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_plane", data_schema=schema, errors=errors)

    @callback
    def _async_save_planes(self, planes: list[dict[str, Any]]) -> ConfigFlowResult:
        """Zapíše nový zoznam plôch do dát záznamu; reload spraví update listener."""
        options = dict(self.config_entry.options)
        # Po zmene počtu plôch sa mohol zmeniť minimálny interval – zdvihneme ho, ak treba.
        minimum = min_update_interval(len(planes), self._has_api_key)
        if int(options.get(CONF_UPDATE_INTERVAL, minimum)) < minimum:
            _LOGGER.info("Interval aktualizácie zvýšený na %d min kvôli počtu plôch", minimum)
            options[CONF_UPDATE_INTERVAL] = minimum
        self.hass.config_entries.async_update_entry(
            self.config_entry, data={**self.config_entry.data, CONF_PLANES: planes}
        )
        _LOGGER.info("Záznam „%s“ má teraz %d plôch", self.config_entry.title, len(planes))
        return self.async_create_entry(data=options)
