"""Test API a výpočtov mimo Home Assistant.

Načíta parametre z `.env` (pozri `.env.example`), zavolá Forecast.Solar pre každú
plochu, zlúči krivky a vypíše súhrn: energiu dnes/zajtra, zostávajúcu výrobu,
špičku a 30-minútový priebeh. Spotrebuje jedno volanie API na plochu.

Spustenie:  python tools/fetch_test.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import importlib
import logging
import os
from pathlib import Path
import sys
import types

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "forecast_solar_plus"

# Balík `custom_components.forecast_solar_plus` importuje Home Assistant v `__init__`,
# preto vytvoríme syntetický balík `fsp`, cez ktorý načítame len potrebné moduly
# (relatívne importy `.const` v api.py sa tak vyriešia bez HA).
_pkg = types.ModuleType("fsp")
_pkg.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
sys.modules["fsp"] = _pkg
api = importlib.import_module("fsp.api")
forecast = importlib.import_module("fsp.forecast")

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("fetch_test")


def load_env(path: Path) -> None:
    """Minimalistické načítanie `.env` (KEY=VALUE, riadky s # sa ignorujú) bez závislostí."""
    if not path.exists():
        _LOGGER.warning("Súbor %s neexistuje – použijú sa premenné prostredia", path)
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_planes(spec: str) -> list[tuple[float, float, float]]:
    """`35:-90:5.0,35:90:5.0` → [(sklon, azimut, kWp), …]."""
    planes = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            dec, az, kwp = (float(x) for x in item.split(":"))
        except ValueError as err:
            raise SystemExit(f"Neplatná plocha „{item}“ (očakáva sa sklon:azimut:kWp): {err}") from err
        planes.append((dec, az, kwp))
    if not planes:
        raise SystemExit("FORECAST_SOLAR_PLANES neobsahuje žiadnu plochu")
    return planes


async def main() -> None:
    load_env(ROOT / ".env")
    lat = float(os.environ.get("FORECAST_SOLAR_LAT", "48.15"))
    lon = float(os.environ.get("FORECAST_SOLAR_LON", "17.11"))
    key = os.environ.get("FORECAST_SOLAR_API_KEY") or None
    planes = parse_planes(os.environ.get("FORECAST_SOLAR_PLANES", "35:0:5"))
    inverter_kw = float(os.environ.get("FORECAST_SOLAR_INVERTER_KW", "0") or 0)

    curves = []
    async with aiohttp.ClientSession() as session:
        client = api.ForecastSolarApi(session, key)
        for index, (dec, az, kwp) in enumerate(planes):
            if index:
                await asyncio.sleep(0.5)
            try:
                estimate = await client.estimate(lat, lon, dec, az, kwp)
            except api.ForecastSolarApiError as err:
                _LOGGER.error("Plocha %s/%s/%s zlyhala: %s", dec, az, kwp, err)
                raise SystemExit(1) from err
            curve = forecast.PowerCurve.from_api_watts(estimate.watts)
            curves.append(curve)
            print(f"Plocha {dec:g}°/{az:g}°/{kwp:g} kWp: {curve}  miesto={estimate.place}  limit={estimate.ratelimit}")

    combined = forecast.combine_curves(curves, inverter_watts=inverter_kw * 1000)
    now = datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)

    print()
    print(f"Zlúčená krivka: {combined}")
    print(f"Dnes:            {combined.energy_wh(today, tomorrow) / 1000:.3f} kWh")
    print(f"Zajtra:          {combined.energy_wh(tomorrow, day_after) / 1000:.3f} kWh")
    print(f"Zostáva dnes:    {combined.energy_wh(now, tomorrow) / 1000:.3f} kWh")
    print(f"Výkon teraz:     {combined.power_at(now):.0f} W")
    peak = combined.peak(today, tomorrow)
    if peak:
        print(f"Špička dnes:     {peak.watts:.0f} W o {peak.time.astimezone():%H:%M}")
    print()
    print("30-min priebeh dnes (len nenulové periódy):")
    for period in combined.periods(today, tomorrow, timedelta(minutes=30)):
        if period.value > 0:
            print(f"  {period.start.astimezone():%H:%M}  {period.value / 1000:.3f} kW")


if __name__ == "__main__":
    asyncio.run(main())
