"""Čistá výpočtová logika predpovede výroby – bez závislosti na Home Assistant.

Forecast.Solar vracia v poli `watts` **okamžitý výkon** vo vybraných časových bodoch
(východ slnka, celé hodiny/štvrťhodiny, západ slnka). Energia medzi bodmi
(`watt_hours_period`) je lichobežníková integrácia, teda API samo modeluje výkon
ako po častiach lineárnu funkciu. Presne tento model tu používame:

* `PowerCurve` – po častiach lineárna krivka výkonu P(t) v UTC,
* súčet kriviek viacerých plôch = krivka so zjednotením bodov (súčet lineárnych
  funkcií je opäť lineárny po častiach),
* orezanie striedačom = min(P(t), P_max) s doplnením priesečníkov,
* energia v intervale = presná integrácia lichobežníkmi,
* priemerný výkon periódy (ako `pv_estimate` v Solcast) = energia periódy / dĺžka periódy.

Všetky časy vo vnútri modulu sú tz-aware a prevedené na UTC, aby aritmetika
s periódami nebola citlivá na zmenu letného času.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

_LOGGER = logging.getLogger(__name__)


def _to_utc(value: datetime) -> datetime:
    """Prevedie tz-aware dátum na UTC; naivný dátum považuje za chybu volajúceho."""
    if value.tzinfo is None:
        raise ValueError("Očakáva sa tz-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Period:
    """Jedna perióda detailnej predpovede.

    `start` – začiatok periódy (UTC), `value` – priemerný výkon vo W alebo
    energia vo Wh podľa toho, ktorá metóda periódu vytvorila.
    """

    start: datetime
    value: float


@dataclass(frozen=True)
class Peak:
    """Špička výkonu: čas (UTC) a hodnota vo W."""

    time: datetime
    watts: float


class PowerCurve:
    """Po častiach lineárna krivka výkonu.

    Body sú zoradené podľa času (UTC). Mimo rozsahu bodov je výkon 0 W – to
    zodpovedá noci medzi západom a východom slnka, kde API body nevracia.
    """

    __slots__ = ("_times", "_watts")

    def __init__(self, points: Iterable[tuple[datetime, float]] = ()) -> None:
        # Zoradíme a odstránime duplicitné časy (ponecháme poslednú hodnotu).
        merged: dict[datetime, float] = {}
        for when, watts in points:
            merged[_to_utc(when)] = float(watts)
        ordered = sorted(merged.items())
        self._times: tuple[datetime, ...] = tuple(t for t, _ in ordered)
        self._watts: tuple[float, ...] = tuple(w for _, w in ordered)

    # ------------------------------------------------------------------ tvorba

    @classmethod
    def from_api_watts(cls, watts: dict[str, float]) -> PowerCurve:
        """Vytvorí krivku z poľa `result.watts` odpovede API (kľúče ISO 8601 s posunom)."""
        points: list[tuple[datetime, float]] = []
        for stamp, value in watts.items():
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                _LOGGER.warning("Nepodarilo sa spracovať časovú značku API: %s", stamp)
                continue
            if when.tzinfo is None:
                # API by malo vracať posun; ak nie, bezpečnejšie je bod preskočiť než hádať.
                _LOGGER.warning("Časová značka API bez časového pásma, preskakujem: %s", stamp)
                continue
            points.append((when, value))
        return cls(points)

    @classmethod
    def from_serialized(cls, data: Sequence[Sequence[object]]) -> PowerCurve:
        """Obnoví krivku z tvaru uloženého v úložisku (`[[iso, watts], …]`)."""
        points: list[tuple[datetime, float]] = []
        for item in data:
            try:
                stamp, value = item
                points.append((datetime.fromisoformat(str(stamp)), float(value)))  # type: ignore[arg-type]
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Poškodený bod v úložisku %s: %s", item, err)
        return cls(points)

    def serialize(self) -> list[list[object]]:
        """Serializuje krivku pre uloženie do JSON úložiska."""
        return [[t.isoformat(), w] for t, w in zip(self._times, self._watts, strict=True)]

    # -------------------------------------------------------------- vlastnosti

    @property
    def is_empty(self) -> bool:
        """True, ak krivka nemá žiadne body."""
        return not self._times

    @property
    def points(self) -> list[tuple[datetime, float]]:
        """Kópia bodov krivky (UTC, W)."""
        return list(zip(self._times, self._watts, strict=True))

    @property
    def start(self) -> datetime | None:
        """Prvý časový bod krivky alebo None."""
        return self._times[0] if self._times else None

    @property
    def end(self) -> datetime | None:
        """Posledný časový bod krivky alebo None."""
        return self._times[-1] if self._times else None

    # ---------------------------------------------------------------- výpočty

    def power_at(self, at: datetime) -> float:
        """Okamžitý výkon vo W v čase `at` (lineárna interpolácia, mimo rozsahu 0)."""
        if not self._times:
            return 0.0
        at = _to_utc(at)
        idx = bisect_right(self._times, at)
        if idx == 0:
            return 0.0
        if idx >= len(self._times):
            # Presne na poslednom bode vraciame jeho hodnotu, za ním už 0.
            return self._watts[-1] if at == self._times[-1] else 0.0
        t0, t1 = self._times[idx - 1], self._times[idx]
        w0, w1 = self._watts[idx - 1], self._watts[idx]
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return w1
        frac = (at - t0).total_seconds() / span
        return w0 + (w1 - w0) * frac

    def energy_wh(self, start: datetime, end: datetime) -> float:
        """Energia vo Wh vyrobená medzi `start` a `end` (presná lichobežníková integrácia)."""
        if not self._times:
            return 0.0
        start, end = _to_utc(start), _to_utc(end)
        # Mimo rozsahu bodov je výkon 0, preto stačí integrovať prienik s rozsahom krivky.
        a = max(start, self._times[0])
        b = min(end, self._times[-1])
        if b <= a:
            return 0.0
        inner = self._times[bisect_right(self._times, a) : bisect_left(self._times, b)]
        total = 0.0
        prev_t, prev_w = a, self.power_at(a)
        for t in (*inner, b):
            w = self.power_at(t)
            total += (prev_w + w) / 2.0 * ((t - prev_t).total_seconds() / 3600.0)
            prev_t, prev_w = t, w
        return total

    def peak(self, start: datetime, end: datetime) -> Peak | None:
        """Špička výkonu v intervale [start, end).

        Maximum po častiach lineárnej funkcie leží v niektorom bode zlomu alebo na
        hranici intervalu, preto stačí prejsť tieto kandidátske body.
        """
        if not self._times:
            return None
        start, end = _to_utc(start), _to_utc(end)
        if end <= start:
            return None
        candidates = [start, *self._times[bisect_right(self._times, start) : bisect_left(self._times, end)]]
        best_t, best_w = start, -1.0
        for t in candidates:
            w = self.power_at(t)
            if w > best_w:
                best_t, best_w = t, w
        if best_w <= 0.0:
            return None
        return Peak(best_t, best_w)

    def periods(self, start: datetime, end: datetime, step: timedelta) -> list[Period]:
        """Rozdelí interval na periódy dĺžky `step` a vráti priemerný výkon (W) každej z nich.

        Zodpovedá `pv_estimate` v Solcast (tam v kW – prevod robí vrstva senzorov).
        """
        start, end = _to_utc(start), _to_utc(end)
        result: list[Period] = []
        t = start
        while t < end:
            # Posledná perióda môže byť skrátená – priemer rátame zo skutočnej dĺžky.
            t_next = min(t + step, end)
            span_h = (t_next - t).total_seconds() / 3600.0
            avg = self.energy_wh(t, t_next) / span_h if span_h > 0 else 0.0
            result.append(Period(t, avg))
            t = t + step
        return result

    def remaining_series(self, start: datetime, end: datetime, step: timedelta) -> list[Period]:
        """Pre každý začiatok periódy vráti energiu (Wh), ktorá ostáva do `end`.

        Toto je „zostávajúca výroba v čase" – klesajúca krivka vhodná do grafu.
        """
        start, end = _to_utc(start), _to_utc(end)
        result: list[Period] = []
        t = start
        while t < end:
            result.append(Period(t, self.energy_wh(t, end)))
            t = t + step
        return result

    # ---------------------------------------------------------------- úpravy

    def clipped(self, max_watts: float) -> PowerCurve:
        """Vráti krivku orezanú zhora na `max_watts` (limit striedača).

        V segmentoch, kde krivka hranicu pretína, sa vloží priesečník, aby ostala
        po častiach lineárna a integrácia zostala presná.
        """
        if max_watts <= 0 or not self._times:
            return self
        points: list[tuple[datetime, float]] = []
        for idx, (t0, w0) in enumerate(zip(self._times, self._watts, strict=True)):
            points.append((t0, min(w0, max_watts)))
            if idx + 1 >= len(self._times):
                break
            t1, w1 = self._times[idx + 1], self._watts[idx + 1]
            # Priesečník existuje, keď je jeden koniec nad a druhý pod hranicou.
            if (w0 - max_watts) * (w1 - max_watts) < 0:
                frac = (max_watts - w0) / (w1 - w0)
                points.append((t0 + (t1 - t0) * frac, max_watts))
        return PowerCurve(points)

    def __add__(self, other: PowerCurve) -> PowerCurve:
        """Súčet dvoch kriviek (zlúčenie plôch do jednej výroby)."""
        if other.is_empty:
            return self
        if self.is_empty:
            return other
        times = sorted(set(self._times) | set(other._times))
        return PowerCurve((t, self.power_at(t) + other.power_at(t)) for t in times)

    def __repr__(self) -> str:
        return f"PowerCurve(points={len(self._times)}, start={self.start}, end={self.end})"


def combine_curves(curves: Iterable[PowerCurve], inverter_watts: float = 0.0) -> PowerCurve:
    """Sčíta krivky všetkých plôch a voliteľne oreže limitom striedača (W)."""
    total = PowerCurve()
    for curve in curves:
        total = total + curve
    return total.clipped(inverter_watts) if inverter_watts > 0 else total
