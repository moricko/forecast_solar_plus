"""Asynchrónny klient pre Forecast.Solar API (endpoint `estimate`).

Zodpovednosť modulu: zostaviť URL pre jednu plochu, zavolať API, rozlíšiť chybové
stavy (rate-limit, neplatný kľúč, chyba servera) a vrátiť surové dáta spolu
s informáciou o limite volaní. Žiadne výpočty predpovede sa tu nerobia.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL, API_TIMEOUT_SECONDS

_LOGGER = logging.getLogger(__name__)


class ForecastSolarApiError(Exception):
    """Všeobecná chyba komunikácie s API."""


class ForecastSolarAuthError(ForecastSolarApiError):
    """Neplatný alebo neoprávnený API kľúč."""


class ForecastSolarRateLimitError(ForecastSolarApiError):
    """Prekročený limit volaní (HTTP 429)."""

    def __init__(self, message: str, ratelimit: RateLimit | None = None) -> None:
        super().__init__(message)
        self.ratelimit = ratelimit


@dataclass(frozen=True)
class RateLimit:
    """Stav limitu volaní podľa odpovede API.

    `limit` – maximálny počet volaní v okne, `remaining` – koľko ostáva,
    `period` – dĺžka kĺzavého okna v sekundách, `reset_at` – odhad, kedy sa
    okno uvoľní (z hlavičky `X-Ratelimit-Reset`, ak ju server poslal).
    """

    limit: int | None = None
    remaining: int | None = None
    period: int | None = None
    reset_at: datetime | None = None
    zone: str | None = None

    @property
    def used(self) -> int | None:
        """Počet využitých volaní v okne (limit − zostatok)."""
        if self.limit is None or self.remaining is None:
            return None
        return max(self.limit - self.remaining, 0)

    def as_dict(self) -> dict[str, Any]:
        """Serializácia pre úložisko/diagnostiku."""
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "period": self.period,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "zone": self.zone,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RateLimit | None:
        """Obnova z úložiska; pri poškodených dátach vráti None."""
        if not data:
            return None
        try:
            reset_raw = data.get("reset_at")
            return cls(
                limit=data.get("limit"),
                remaining=data.get("remaining"),
                period=data.get("period"),
                reset_at=datetime.fromisoformat(reset_raw) if reset_raw else None,
                zone=data.get("zone"),
            )
        except (ValueError, TypeError) as err:
            _LOGGER.warning("Poškodený záznam rate-limitu v úložisku: %s", err)
            return None


@dataclass(frozen=True)
class PlaneEstimate:
    """Surová odpoveď API pre jednu plochu.

    `watts` – slovník ISO čas → okamžitý výkon vo W (kľúč `result.watts`),
    `ratelimit` – stav limitu po tomto volaní, `place`/`timezone` – informácie
    o polohe z `message.info` (len na diagnostiku).
    """

    watts: dict[str, float]
    fetched_at: datetime
    ratelimit: RateLimit | None = None
    place: str | None = None
    timezone: str | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


class ForecastSolarApi:
    """Tenký klient nad `aiohttp.ClientSession`.

    Session sa nevlastní – v Home Assistant ju poskytuje `async_get_clientsession`,
    v samostatnom skripte ju vytvorí volajúci.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str | None = None,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._api_key = (api_key or "").strip() or None
        self._base_url = base_url.rstrip("/")

    @property
    def has_api_key(self) -> bool:
        """True, ak sa volá API s kľúčom (vyšší limit a rozlíšenie)."""
        return self._api_key is not None

    def build_url(self, latitude: float, longitude: float, declination: float, azimuth: float, kwp: float) -> str:
        """Zostaví cestu endpointu `estimate` pre jednu plochu.

        Sklon a azimut API prijíma ako celé čísla, súradnice na 4 desatinné miesta.
        """
        prefix = f"{self._base_url}/{self._api_key}" if self._api_key else self._base_url
        return (
            f"{prefix}/estimate/{latitude:.4f}/{longitude:.4f}/{int(round(declination))}/{int(round(azimuth))}/{kwp:g}"
        )

    async def estimate(
        self,
        latitude: float,
        longitude: float,
        declination: float,
        azimuth: float,
        kwp: float,
        *,
        damping_morning: float = 0.0,
        damping_evening: float = 0.0,
        resolution: str | None = None,
    ) -> PlaneEstimate:
        """Získa predpoveď pre jednu plochu.

        Parametre tlmenia (`damping_*`) sa posielajú len ak sú nenulové, `resolution`
        len s API kľúčom – verejný plán ho ignoruje a zbytočne by predlžoval URL.
        """
        url = self.build_url(latitude, longitude, declination, azimuth, kwp)
        params: dict[str, str] = {"time": "iso8601"}
        if damping_morning:
            params["damping_morning"] = f"{damping_morning:g}"
        if damping_evening:
            params["damping_evening"] = f"{damping_evening:g}"
        if resolution and self._api_key:
            params["resolution"] = str(resolution)

        _LOGGER.debug("Volám Forecast.Solar: %s params=%s", self._redact(url), params)
        try:
            async with asyncio.timeout(API_TIMEOUT_SECONDS):
                async with self._session.get(url, params=params) as response:
                    ratelimit = self._parse_ratelimit_headers(response)
                    text = await response.text()
                    payload = self._parse_json(text)
                    # Telo odpovede nesie presnejší rate-limit než hlavičky – uprednostníme ho.
                    ratelimit = self._merge_body_ratelimit(payload, ratelimit)
                    self._raise_for_status(response.status, payload, ratelimit)
        except TimeoutError as err:
            raise ForecastSolarApiError(f"Časový limit {API_TIMEOUT_SECONDS}s pri volaní API vypršal") from err
        except aiohttp.ClientError as err:
            raise ForecastSolarApiError(f"Chyba spojenia s Forecast.Solar: {err}") from err

        result = payload.get("result") or {}
        watts = result.get("watts")
        if not isinstance(watts, dict):
            raise ForecastSolarApiError("Odpoveď API neobsahuje pole result.watts")

        info = (payload.get("message") or {}).get("info") or {}
        _LOGGER.debug(
            "Forecast.Solar OK: %d bodov, limit %s/%s",
            len(watts),
            ratelimit.remaining if ratelimit else "?",
            ratelimit.limit if ratelimit else "?",
        )
        return PlaneEstimate(
            watts={str(k): float(v) for k, v in watts.items()},
            fetched_at=datetime.now(timezone.utc),
            ratelimit=ratelimit,
            place=info.get("place"),
            timezone=info.get("timezone"),
            raw_message=payload.get("message") or {},
        )

    # ----------------------------------------------------------- pomocné metódy

    def _redact(self, url: str) -> str:
        """Odstráni API kľúč z URL pre potreby logovania."""
        return url.replace(self._api_key, "***") if self._api_key else url

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Bezpečné parsovanie JSON; pri chybe vráti prázdny slovník (stav rieši volajúci)."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            _LOGGER.warning("Odpoveď API nie je platný JSON: %s (%s…)", err, text[:120])
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_ratelimit_headers(response: aiohttp.ClientResponse) -> RateLimit | None:
        """Prečíta hlavičky `X-Ratelimit-*`; chýbajúce hodnoty ponechá None."""
        headers = response.headers

        def _int(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value) if value is not None else None
            except ValueError:
                _LOGGER.debug("Neplatná hlavička %s=%s", name, value)
                return None

        limit = _int("X-Ratelimit-Limit")
        remaining = _int("X-Ratelimit-Remaining")
        reset_seconds = _int("X-Ratelimit-Reset")
        if limit is None and remaining is None and reset_seconds is None:
            return None
        reset_at = (
            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=reset_seconds)
            if reset_seconds is not None
            else None
        )
        return RateLimit(limit=limit, remaining=remaining, reset_at=reset_at)

    @staticmethod
    def _merge_body_ratelimit(payload: dict[str, Any], header_limit: RateLimit | None) -> RateLimit | None:
        """Doplní rate-limit z tela odpovede (`message.ratelimit`), hlavičky slúžia ako záloha."""
        body = (payload.get("message") or {}).get("ratelimit") or {}
        if not body:
            return header_limit
        try:
            return RateLimit(
                limit=int(body.get("limit"))
                if body.get("limit") is not None
                else (header_limit.limit if header_limit else None),
                remaining=int(body.get("remaining"))
                if body.get("remaining") is not None
                else (header_limit.remaining if header_limit else None),
                period=int(body.get("period")) if body.get("period") is not None else None,
                reset_at=header_limit.reset_at if header_limit else None,
                zone=body.get("zone"),
            )
        except (TypeError, ValueError) as err:
            _LOGGER.warning("Nepodarilo sa prečítať rate-limit z odpovede: %s", err)
            return header_limit

    @staticmethod
    def _raise_for_status(status: int, payload: dict[str, Any], ratelimit: RateLimit | None) -> None:
        """Premení HTTP stav na výnimku s textom chyby z tela odpovede."""
        if status == 200:
            return
        message = (payload.get("message") or {}).get("text") or f"HTTP {status}"
        if status == 429:
            raise ForecastSolarRateLimitError(f"Prekročený limit volaní Forecast.Solar: {message}", ratelimit)
        if status in (401, 403):
            raise ForecastSolarAuthError(f"Forecast.Solar odmietol API kľúč: {message}")
        raise ForecastSolarApiError(f"Forecast.Solar vrátil chybu {status}: {message}")
