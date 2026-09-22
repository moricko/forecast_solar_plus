# Forecast.Solar Plus – integrácia pre Home Assistant

Vlastná integrácia (custom component), ktorá poskytuje predpoveď výroby fotovoltiky
z [Forecast.Solar](https://forecast.solar) **v štýle Solcast PV Forecast**:

- **priebeh výroby v čase** – atribúty `detailedForecast` (30 min) a `detailedHourly` (60 min)
  s rovnakou štruktúrou ako Solcast (`period_start`, `pv_estimate` v kW), použiteľné priamo
  v apexcharts a podobných kartách,
- **zostávajúca výroba v čase** – senzor `forecast_remaining_today` so stavom v kWh
  a atribútom s klesajúcou krivkou zostávajúcej energie po periódach,
- **zlúčenie viacerých orientácií (plôch) do jednej výroby** – každá plocha sa volá
  samostatne (funguje aj bez API kľúča) a krivky sa sčítajú lokálne; voliteľne s orezaním
  limitom striedača,
- senzory *výkon teraz / o 30 min / o 1 h*, *táto/ďalšia hodina*, *špička dnes/zajtra*,
  diagnostika limitu API,
- **medzipamäť** v `.storage` – po reštarte HA sa nevolá API zbytočne,
- lokálny minútový prepočet senzorov viazaných na aktuálny čas bez volania API,
- služby `query_forecast_data` a `force_update_forecasts` ako v Solcaste.

Účel: záloha k Solcastu (výpadok) alebo častejšia aktualizácia, než Solcast dovoľuje.

| | |
| --- | --- |
| **Začiatok projektu** | 2026-09-21 |
| **Posledná zmena** | 2026-09-21 14:25 |
| **Verzia** | 1.0.0 |
| **Minimálna verzia HA** | 2025.1 |

## Inštalácia

### HACS (vlastný repozitár)

1. HACS → Integrations → ⋮ → *Custom repositories* → URL tohto repozitára, kategória *Integration*.
2. Nainštaluj **Forecast.Solar Plus** a reštartuj Home Assistant.

### Ručne

Skopíruj adresár `custom_components/forecast_solar_plus` do `config/custom_components/`
v Home Assistant a reštartuj HA.

## Nastavenie (UI)

*Nastavenia → Zariadenia a služby → Pridať integráciu → Forecast.Solar Plus*

| Krok | Polia | Predvolené |
| --- | --- | --- |
| 1. Poloha | názov, zemepisná šírka/dĺžka, API kľúč (voliteľný) | poloha z nastavení HA, bez kľúča |
| 2. Plocha (opakovateľný) | názov, sklon 0–90°, azimut −180…180° (−90 = východ, 0 = juh, 90 = západ), kWp, „pridať ďalšiu" | 30°, 0°, 5 kWp |

Po dokončení sa spraví jedno overovacie volanie API pre prvú plochu.

### Možnosti (ozubené koliesko)

| Možnosť | Význam | Predvolené |
| --- | --- | --- |
| Interval aktualizácie | minúty medzi volaniami API; minimum sa počíta z limitu plánu a počtu plôch (80 % limitu) | 15 min bez kľúča, 10 min s kľúčom |
| Tlmenie ráno / večer | parameter `damping_morning` / `damping_evening` API (−0.25 … 5) | 0 |
| Limit striedača | kW; zlúčená krivka sa oreže zhora, 0 = bez limitu | 0 |
| Rozlíšenie | len s API kľúčom: 15/30/60 min alebo podľa účtu | podľa účtu |
| Pridať / odstrániť plochu | zmena zoznamu plôch (integrácia sa znovu načíta) | – |

### Limity Forecast.Solar

| Plán | Volaní / 60 min (kĺzavo) | Rozlíšenie | Dní | Plôch v 1 volaní |
| --- | --- | --- | --- | --- |
| verejný (bez kľúča) | 12 / IP | 1 h | 2 | 1 |
| Personal | 60 / kľúč | 30 min | 4 | 1 |
| Personal Plus | 60 / kľúč | 15 min | 4 | 2 |

Integrácia volá API **raz na plochu**, preto pri 2 plochách bez kľúča je bezpečné
minimum ~13 min (predvolené 15 min = 8 volaní/h, ostáva rezerva na ručné obnovenie).
Hodinové body verejného plánu sa lineárne interpolujú na 30-min periódy – rovnako
ako to robí samotné API pri výpočte `watt_hours_period`.

## Entity

Zariadenie *Forecast.Solar Plus* (súhrn všetkých plôch) a podriadené zariadenie pre každú plochu.

| Senzor | Jednotka | Atribúty | Aj pre plochu |
| --- | --- | --- | --- |
| `forecast_today`, `forecast_tomorrow` | kWh | `detailedForecast`, `detailedHourly`, `dayname`, `dataCorrect` | ✓ |
| `forecast_day_3`, `forecast_day_4` (predvolene vypnuté; len s API kľúčom majú dáta) | kWh | ako vyššie | |
| `forecast_remaining_today` | kWh | `detailedForecast`, `detailedHourly` – zostávajúca energia (kWh) od každej periódy do konca dňa | ✓ |
| `forecast_this_hour`, `forecast_next_hour` | kWh | | |
| `power_now`, `power_now_30m`, `power_now_1h` | W | | `power_now` ✓ |
| `peak_forecast_today`, `peak_time_today`, `peak_forecast_tomorrow`, `peak_time_tomorrow` | W / čas | | dnes ✓ |
| `api_last_polled` (diag.) | čas | `from_cache`, `last_error`, `place` | |
| `api_limit`, `api_used`, `api_remaining` (diag.) | – | | |

ID entít: `sensor.forecast_solar_plus_forecast_today`, pre plochu
`sensor.forecast_solar_plus_<plocha>_forecast_today` (podľa názvu záznamu a plochy).

Tvar atribútu (zhodný so Solcast; `pv_estimate10/90` Forecast.Solar neposkytuje):

```yaml
detailedForecast:
  - period_start: "2026-09-21T06:30:00+02:00"
    pv_estimate: 0.211   # kW – priemerný výkon periódy
```

### Príklad apexcharts (prepnutie zo Solcastu)

```yaml
type: custom:apexcharts-card
graph_span: 24h
span:
  start: day
series:
  - entity: sensor.forecast_solar_plus_forecast_today
    name: Predpoveď
    type: area
    data_generator: |
      return entity.attributes.detailedForecast.map(p => [new Date(p.period_start), p.pv_estimate]);
  - entity: sensor.forecast_solar_plus_forecast_remaining_today
    name: Zostáva (kWh)
    type: line
    yaxis_id: kwh
    data_generator: |
      return entity.attributes.detailedForecast.map(p => [new Date(p.period_start), p.pv_estimate]);
```

## Služby

| Služba | Parametre | Výsledok |
| --- | --- | --- |
| `forecast_solar_plus.query_forecast_data` | `start_date_time`, `end_date_time`, voliteľne `plane` (názov plochy), `config_entry_id` | `data: [{period_start, pv_estimate}]` – 30-min periódy v kW |
| `forecast_solar_plus.force_update_forecasts` | voliteľne `config_entry_id` | okamžité volanie API (míňa limit) |

## Logovanie

Integrácia loguje cez štandardný logger HA (`DEBUG` volania API a použitie medzipamäte,
`INFO` aktualizácie a zmeny konfigurácie, `WARNING` rate-limit a poškodenú medzipamäť,
`ERROR` chyby API a výpočtov):

```yaml
logger:
  logs:
    custom_components.forecast_solar_plus: debug
```

## Vývoj a testy

Závislosti pre vývoj: Python ≥ 3.12, `pytest`, `aiohttp`; na integračné testy navyše
`pytest-homeassistant-custom-component` (vyžaduje Linux/WSL – Home Assistant sa na Windows nenainštaluje).

```bash
python -m pip install pytest aiohttp
python -m pytest tests -q
```

Jednotkové testy (`tests/test_forecast.py`) bežia všade; integračné testy
(`tests/test_integration.py` – reálny setup config entry, senzory, služby, možnosti,
medzipamäť, zlyhanie API) sa bez nainštalovaného HA automaticky preskočia:

```bash
pip install homeassistant pytest-homeassistant-custom-component ruff
python -m pytest tests -q
ruff check custom_components tests tools
```

Test proti reálnemu API mimo HA (spotrebuje 1 volanie na plochu) – parametre v `.env`
podľa [`.env.example`](.env.example):

```bash
python tools/fetch_test.py
```

Štruktúra a výpočtový model: [architecture.md](architecture.md). Denník zmien: [history.md](history.md).
