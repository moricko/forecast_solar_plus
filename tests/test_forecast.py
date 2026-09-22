"""Jednotkové testy čistej výpočtovej logiky `forecast.py`.

Modul sa načítava priamo zo súboru, aby testy nevyžadovali nainštalovaný
Home Assistant (balík `custom_components.forecast_solar_plus` ho importuje v `__init__`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "forecast_solar_plus" / "forecast.py"
_spec = importlib.util.spec_from_file_location("fsp_forecast", _MODULE_PATH)
forecast = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# dataclasses vyžadujú modul v sys.modules (riešia cez neho anotácie)
sys.modules[_spec.name] = forecast
_spec.loader.exec_module(forecast)

PowerCurve = forecast.PowerCurve
combine_curves = forecast.combine_curves

TZ = ZoneInfo("Europe/Bratislava")
UTC = timezone.utc


def _t(hour: int, minute: int = 0, day: int = 21) -> datetime:
    """Lokálny čas 2026-09-<day> HH:MM (letný čas, +02:00)."""
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


# Skutočná odpoveď verejného API pre Bratislavu (skrátená) – overená proti watt_hours_period.
SAMPLE_WATTS = {
    "2026-09-21T06:36:51+02:00": 0,
    "2026-09-21T07:00:00+02:00": 272,
    "2026-09-21T08:00:00+02:00": 706,
    "2026-09-21T09:00:00+02:00": 1164,
    "2026-09-21T10:00:00+02:00": 1495,
    "2026-09-21T11:00:00+02:00": 1853,
    "2026-09-21T12:00:00+02:00": 2350,
    "2026-09-21T13:00:00+02:00": 2372,
    "2026-09-21T14:00:00+02:00": 2367,
    "2026-09-21T15:00:00+02:00": 2060,
    "2026-09-21T16:00:00+02:00": 1521,
    "2026-09-21T17:00:00+02:00": 877,
    "2026-09-21T18:00:00+02:00": 443,
    "2026-09-21T18:52:25+02:00": 0,
}
SAMPLE_DAY_WH = 17371  # watt_hours_day z tej istej odpovede


@pytest.fixture
def sample() -> PowerCurve:
    return PowerCurve.from_api_watts(SAMPLE_WATTS)


def test_from_api_parses_all_points(sample: PowerCurve) -> None:
    assert not sample.is_empty
    assert len(sample.points) == len(SAMPLE_WATTS)
    # Body sú v UTC a zoradené
    times = [t for t, _ in sample.points]
    assert times == sorted(times)
    assert all(t.tzinfo == UTC for t in times)


def test_from_api_skips_bad_timestamps() -> None:
    curve = PowerCurve.from_api_watts({"nezmysel": 5, "2026-09-21T12:00:00": 7, "2026-09-21T13:00:00+02:00": 9})
    # neplatný formát a naivný čas sa preskočia, ostane jeden bod
    assert len(curve.points) == 1


def test_energy_matches_api_watt_hours_period(sample: PowerCurve) -> None:
    # API: watt_hours_period[08:00] = 489 Wh, t. j. (272+706)/2 za hodinu
    assert sample.energy_wh(_t(7), _t(8)) == pytest.approx(489, abs=1)
    # Prvá perióda od východu slnka: (0+272)/2 * 23.15 min
    assert sample.energy_wh(_t(6, 36) + timedelta(seconds=51), _t(7)) == pytest.approx(52, abs=1)


def test_energy_whole_day_matches_api(sample: PowerCurve) -> None:
    day_start = _t(0)
    day_end = day_start + timedelta(days=1)
    assert sample.energy_wh(day_start, day_end) == pytest.approx(SAMPLE_DAY_WH, rel=0.001)
    # Mimo rozsahu krivky je energia 0
    assert sample.energy_wh(_t(0), _t(6)) == 0
    assert sample.energy_wh(_t(19), _t(23)) == 0


def test_power_at_interpolates(sample: PowerCurve) -> None:
    assert sample.power_at(_t(7)) == 272
    assert sample.power_at(_t(7, 30)) == pytest.approx((272 + 706) / 2)
    assert sample.power_at(_t(3)) == 0
    assert sample.power_at(_t(23)) == 0
    # Presne na poslednom bode
    assert sample.power_at(datetime.fromisoformat("2026-09-21T18:52:25+02:00")) == 0


def test_energy_is_additive(sample: PowerCurve) -> None:
    whole = sample.energy_wh(_t(6), _t(20))
    parts = sum(
        sample.energy_wh(_t(6) + timedelta(minutes=17 * i), _t(6) + timedelta(minutes=17 * (i + 1))) for i in range(60)
    )
    assert parts == pytest.approx(whole, rel=1e-9)


def test_peak(sample: PowerCurve) -> None:
    peak = sample.peak(_t(0), _t(0) + timedelta(days=1))
    assert peak is not None
    assert peak.watts == 2372
    assert peak.time == _t(13).astimezone(UTC)
    # Prázdny interval / noc
    assert sample.peak(_t(0), _t(5)) is None
    assert sample.peak(_t(12), _t(12)) is None


def test_periods_30min_average(sample: PowerCurve) -> None:
    periods = sample.periods(_t(0), _t(0) + timedelta(days=1), timedelta(minutes=30))
    assert len(periods) == 48
    assert periods[0].start == _t(0).astimezone(UTC)
    # 12:00–12:30 lineárne od 2350 do 2361 → priemer ≈ 2355.5
    p1200 = next(p for p in periods if p.start == _t(12).astimezone(UTC))
    assert p1200.value == pytest.approx((2350 + 2361) / 2, abs=0.5)
    # Súčet energie periód = energia dňa
    total = sum(p.value * 0.5 for p in periods)
    assert total == pytest.approx(SAMPLE_DAY_WH, rel=0.001)


def test_remaining_series_is_decreasing(sample: PowerCurve) -> None:
    day_end = _t(0) + timedelta(days=1)
    series = sample.remaining_series(_t(0), day_end, timedelta(minutes=30))
    values = [p.value for p in series]
    assert values[0] == pytest.approx(SAMPLE_DAY_WH, rel=0.001)
    assert all(a >= b for a, b in zip(values, values[1:], strict=False))
    assert values[-1] == 0


def test_combine_two_planes_sums_power() -> None:
    east = PowerCurve([(_t(6), 0), (_t(9), 3000), (_t(12), 1000), (_t(15), 0)])
    west = PowerCurve([(_t(9), 0), (_t(12), 1000), (_t(15), 3000), (_t(18), 0)])
    total = combine_curves([east, west])
    # Body sú zjednotené a výkon sa sčítal
    assert total.power_at(_t(9)) == 3000
    assert total.power_at(_t(12)) == 2000
    assert total.power_at(_t(15)) == 3000
    assert total.power_at(_t(10, 30)) == pytest.approx(east.power_at(_t(10, 30)) + west.power_at(_t(10, 30)))
    # Energia je súčtom energií
    e = east.energy_wh(_t(0), _t(23)) + west.energy_wh(_t(0), _t(23))
    assert total.energy_wh(_t(0), _t(23)) == pytest.approx(e)


def test_combine_with_empty_curve(sample: PowerCurve) -> None:
    assert (sample + PowerCurve()).points == sample.points
    assert (PowerCurve() + sample).points == sample.points
    assert combine_curves([]).is_empty


def test_inverter_clipping_inserts_crossings() -> None:
    curve = PowerCurve([(_t(6), 0), (_t(12), 4000), (_t(18), 0)])
    clipped = curve.clipped(2000)
    # Nad limitom je plochá časť
    assert clipped.power_at(_t(12)) == 2000
    assert clipped.power_at(_t(10)) == 2000
    # Priesečníky v 09:00 a 15:00 (lineárny nábeh 0→4000 za 6 h)
    assert clipped.power_at(_t(9)) == pytest.approx(2000)
    assert clipped.power_at(_t(8)) == pytest.approx(4000 * 2 / 6)
    # Energia = pôvodná mínus orezaný trojuholník: 24000 - (2000*6/2) = 18000 Wh
    assert clipped.energy_wh(_t(0), _t(23)) == pytest.approx(18000)
    # Limit 0 = bez zmeny
    assert curve.clipped(0) is curve


def test_serialize_roundtrip(sample: PowerCurve) -> None:
    restored = PowerCurve.from_serialized(sample.serialize())
    assert restored.points == sample.points


def test_naive_datetime_rejected(sample: PowerCurve) -> None:
    with pytest.raises(ValueError):
        sample.power_at(datetime(2026, 9, 21, 12))


def test_dst_change_day_periods() -> None:
    """Deň zmeny času (25.10.2026) má 25 hodín – periódy sa rátajú v UTC bez medzier."""
    tz_day = datetime(2026, 10, 25, tzinfo=TZ)
    next_day = datetime(2026, 10, 26, tzinfo=TZ)
    curve = PowerCurve(
        [
            (datetime(2026, 10, 25, 8, tzinfo=TZ), 0),
            (datetime(2026, 10, 25, 12, tzinfo=TZ), 1000),
            (datetime(2026, 10, 25, 16, tzinfo=TZ), 0),
        ]
    )
    periods = curve.periods(tz_day, next_day, timedelta(minutes=30))
    assert len(periods) == 50
    starts = [p.start for p in periods]
    assert all((b - a) == timedelta(minutes=30) for a, b in zip(starts, starts[1:], strict=False))
    assert sum(p.value * 0.5 for p in periods) == pytest.approx(4000)
