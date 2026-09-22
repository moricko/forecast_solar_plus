# História úloh – Forecast.Solar Plus

Sekvenčný denník všetkých zadaní. Každý záznam má identifikátor, ktorý sa uvádza v git commite.

---

## TASK-20260921-001 · 2026-09-21 13:20

**Zadanie:** Vytvoriť integráciu Home Assistant pre Forecast.Solar v štýle Solcast PV
Forecast: priebeh výroby v čase, zostávajúca výroba v čase a zlúčenie dvoch orientácií
do jednej výroby (aktuálna vstavaná integrácia to nemá). Použitie ako záloha pri výpadku
Solcastu alebo pri potrebe častejšej aktualizácie. Verejný plán (bez kľúča), inštalácia
cez HACS/ručne, názvy atribútov kompatibilné so Solcastom.

**Implementované:**

- Overený model API: `result.watts` sú okamžité výkony v bodoch, `watt_hours_period`
  lichobežníková integrácia → krivka výkonu je po častiach lineárna (`forecast.py`,
  `PowerCurve`: interpolácia, presná integrácia, súčet plôch, orezanie striedačom,
  30-min periódy, zostávajúca séria, špička; všetko v UTC).
- `api.py` – aiohttp klient endpointu `estimate` (verejný/kľúč), `time=iso8601`,
  tlmenie, rozlíšenie, rate-limit z tela odpovede aj hlavičiek, typy chýb
  (auth / rate-limit / všeobecná).
- `coordinator.py` – jedno volanie na plochu, zlúčenie, medzipamäť v `.storage`
  (po reštarte bez volania API), ponechanie posledných dát pri chybe, minútový lokálny tik,
  vynútená aktualizácia.
- `sensor.py` – senzory ako v Solcaste (`forecast_today/tomorrow/day_3/day_4`,
  `forecast_remaining_today` s klesajúcou krivkou, `forecast_this_hour/next_hour`,
  `power_now/_30m/_1h`, `peak_*`, `api_*`), atribúty `detailedForecast` / `detailedHourly`
  (`period_start`, `pv_estimate` kW); vybrané senzory aj pre každú plochu (vlastné zariadenie).
- `config_flow.py` – sprievodca poloha → plochy (opakovateľný krok) → overenie API;
  možnosti (interval s výpočtom bezpečného minima podľa limitu, tlmenie, striedač,
  rozlíšenie, pridanie/odstránenie plochy); reauth pri odmietnutom kľúči.
- Služby `query_forecast_data` (30-min periódy medzi časmi, voliteľne pre plochu)
  a `force_update_forecasts`; `diagnostics.py`; preklady en + sk; `hacs.json`.
- Testy `tests/test_forecast.py` (15 jednotkových, vrátane zhody s `watt_hours_day` reálnej
  odpovede a dňa zmeny času) a `tests/test_integration.py` (7 integračných v testovacom HA
  2026.9: setup, senzory súhrn + plochy, limit striedača, služby, medzipamäť po reloade,
  zlyhanie API, config flow, options flow); `tools/fetch_test.py` – test proti API mimo HA
  (overené s 2 plochami východ/západ). Lint `ruff` bez nálezov.
- Dokumentácia: `readme.md`, `architecture.md`, `.env.example`, `.gitignore`.
