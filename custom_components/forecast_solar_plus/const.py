"""Konštanty integrácie Forecast.Solar Plus.

Všetky kľúče konfigurácie, predvolené hodnoty a limity API sú na jednom mieste,
aby sa dali meniť bez zásahu do logiky.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

# Doména integrácie – musí byť odlišná od vstavanej `forecast_solar`, aby mohli bežať súbežne.
DOMAIN: Final = "forecast_solar_plus"

# Základná adresa API. Verejný prístup: /estimate/…, s kľúčom: /<kľúč>/estimate/…
API_BASE_URL: Final = "https://api.forecast.solar"

# --- Kľúče konfigurácie (config entry data) ---
CONF_API_KEY: Final = "api_key"
CONF_PLANES: Final = "planes"
CONF_PLANE_ID: Final = "id"
CONF_PLANE_NAME: Final = "name"
CONF_DECLINATION: Final = "declination"
CONF_AZIMUTH: Final = "azimuth"
CONF_KWP: Final = "kwp"

# --- Kľúče možností (config entry options) ---
CONF_UPDATE_INTERVAL: Final = "update_interval"  # minúty medzi volaniami API
CONF_DAMPING_MORNING: Final = "damping_morning"
CONF_DAMPING_EVENING: Final = "damping_evening"
CONF_INVERTER_KW: Final = "inverter_kw"  # 0 = bez obmedzenia
CONF_RESOLUTION: Final = "resolution"  # len s API kľúčom: 15 / 30 / 60 minút, "" = podľa účtu

# --- Limity API podľa plánu (rolling 60 minút) ---
RATE_LIMIT_PUBLIC: Final = 12  # volaní za hodinu na IP bez kľúča
RATE_LIMIT_PERSONAL: Final = 60  # volaní za hodinu s kľúčom (Personal)
RATE_LIMIT_SAFETY_MARGIN: Final = 0.8  # využívame max. 80 % limitu, aby ostala rezerva na ručné obnovenie

# Predvolené intervaly aktualizácie (minúty)
DEFAULT_UPDATE_INTERVAL_PUBLIC: Final = 15
DEFAULT_UPDATE_INTERVAL_KEY: Final = 10
MAX_UPDATE_INTERVAL: Final = 240

# Predvolené hodnoty možností
DEFAULT_DAMPING: Final = 0.0
DEFAULT_INVERTER_KW: Final = 0.0
DEFAULT_RESOLUTION: Final = ""
RESOLUTION_OPTIONS: Final = ["", "15", "30", "60"]

# Dĺžka periódy v detailnej predpovedi – 30 minút zodpovedá Solcast (`detailedForecast`).
FORECAST_PERIOD: Final = timedelta(minutes=30)
HOURLY_PERIOD: Final = timedelta(hours=1)

# Koľko dní predpovede sledujeme (verejný plán poskytuje 2, Personal 4).
FORECAST_DAYS: Final = 4

# Ako často sa prepočítavajú senzory viazané na aktuálny čas (bez volania API).
LOCAL_TICK_SECONDS: Final = 60

# Timeout jedného HTTP volania.
API_TIMEOUT_SECONDS: Final = 30

# Krátka pauza medzi volaniami pre jednotlivé plochy (ohľaduplnosť k API).
API_PLANE_DELAY_SECONDS: Final = 0.5

# Názvy atribútov senzorov – zhodné so Solcast PV Forecast, aby sa dali karty len prepnúť.
ATTR_DETAILED_FORECAST: Final = "detailedForecast"
ATTR_DETAILED_HOURLY: Final = "detailedHourly"
ATTR_PERIOD_START: Final = "period_start"
ATTR_PV_ESTIMATE: Final = "pv_estimate"
ATTR_DAY_NAME: Final = "dayname"
ATTR_DATA_CORRECT: Final = "dataCorrect"

# Služby
SERVICE_QUERY_FORECAST_DATA: Final = "query_forecast_data"
SERVICE_FORCE_UPDATE: Final = "force_update_forecasts"
ATTR_START_DATE_TIME: Final = "start_date_time"
ATTR_END_DATE_TIME: Final = "end_date_time"
ATTR_PLANE: Final = "plane"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

# Verzia úložiska medzipamäte (.storage)
STORAGE_VERSION: Final = 1
