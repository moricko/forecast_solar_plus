# Architektúra – Forecast.Solar Plus

## Prehľad

```
Home Assistant
 └─ config entry (data: poloha, kľúč, plochy; options: interval, tlmenie, striedač)
     └─ ForecastSolarPlusCoordinator (coordinator.py)
         ├─ ForecastSolarApi (api.py)  ──HTTP──▶  api.forecast.solar/estimate/…   (1 volanie / plocha)
         ├─ PowerCurve × plocha  ──▶ combine_curves() ──▶ zlúčená krivka (forecast.py)
         ├─ Store  ◀──▶ .storage/forecast_solar_plus.<entry_id>   (medzipamäť posledných kriviek)
         └─ minútový tik ──▶ async_update_listeners()  (prepočet senzorov bez API)
             └─ sensor.py: ForecastSolarPlusSensor × popis × (súhrn | plocha)
 └─ služby (__init__.py): query_forecast_data, force_update_forecasts
```

## Moduly

| Modul | Zodpovednosť | Závislosť na HA |
| --- | --- | --- |
| `const.py` | kľúče konfigurácie, predvolené hodnoty, limity API, názvy atribútov | nie |
| `forecast.py` | čistý výpočtový model `PowerCurve` (interpolácia, integrácia, súčet, orezanie, periódy, špička) | nie |
| `api.py` | HTTP klient `estimate`, parsovanie odpovede a rate-limitu, typy chýb | nie (len aiohttp) |
| `coordinator.py` | plánovanie volaní, medzipamäť, zlúčenie plôch, lokálny tik | áno |
| `sensor.py` | popisy senzorov (`value_fn`, `attr_fn`), časové okná, formát atribútov | áno |
| `config_flow.py` | UI sprievodca (poloha → plochy → overenie), možnosti, reauth | áno |
| `__init__.py` | setup/unload záznamu, služby, reload pri zmene možností | áno |
| `diagnostics.py` | export stavu bez citlivých údajov | áno |

Moduly bez závislosti na HA (`forecast.py`, `api.py`, `const.py`) sa dajú testovať
samostatne – `tests/` a `tools/fetch_test.py` ich načítavajú priamo zo súboru.

## Výpočtový model (`forecast.py`)

Forecast.Solar vracia `result.watts` = **okamžitý výkon v časových bodoch** (východ slnka,
celé hodiny/štvrťhodiny, západ slnka). Overené na reálnej odpovedi:
`watt_hours_period[08:00] = 489 = (272 + 706) / 2` – API samo integruje lichobežníkmi.

Preto je krivka výkonu modelovaná ako **po častiach lineárna funkcia** `P(t)`:

| Operácia | Implementácia | Presnosť |
| --- | --- | --- |
| výkon v čase | lineárna interpolácia medzi susednými bodmi; mimo rozsahu 0 W | presná v modeli API |
| energia v intervale | lichobežníková integrácia so všetkými bodmi zlomu v intervale | presná (súčet periód dňa = `watt_hours_day` API) |
| súčet plôch | zjednotenie bodov, súčet `P(t)` v každom bode | presná (súčet lineárnych funkcií je lineárny po častiach) |
| orezanie striedačom | `min(P, Pmax)` s vložením priesečníkov | presná |
| 30-min periódy (`pv_estimate`) | energia periódy / dĺžka periódy | priemerný výkon periódy ako v Solcaste |
| zostávajúca výroba | pre každý začiatok periódy energia do konca dňa | klesajúca krivka |
| špička | maximum v bodoch zlomu a na hraniciach intervalu | presná |

Všetky časy vnútri modelu sú **UTC**; hranice dní a hodín dodáva vrstva senzorov
z lokálneho času HA (`dt_util.start_of_local_day`), takže zmena letného času nespôsobí
medzery ani duplicity (test `test_dst_change_day_periods`).

## Tok dát

1. `async_setup_entry` → koordinátor načíta medzipamäť (`async_load_cache`), spraví prvú
   aktualizáciu a spustí minútový tik.
2. `_async_update_data`:
   - ak je medzipamäť mladšia než interval a nejde o vynútenú aktualizáciu → vráti dáta z nej
     (žiadne volanie API – typicky po reštarte),
   - inak volá API postupne pre každú plochu (pauza 0,5 s); po úspechu uloží medzipamäť,
   - pri rate-limite/chybe API ponechá posledné známe dáta (`from_cache=True`,
     `last_error`), bez dát vyhodí `UpdateFailed`; neplatný kľúč → `ConfigEntryAuthFailed` → reauth.
3. `ForecastSolarData` obsahuje krivky plôch a zlúčenú krivku; senzory z nej čítajú
   hodnoty vždy **v momente čítania stavu** s aktuálnym časom, minútový tik ich len prinúti
   znovu sa prečítať.

## Konfiguračné dáta

```
entry.data = {
  name, latitude, longitude, api_key ("" = verejný plán),
  planes: [{ id: "8 hex", name, declination, azimuth, kwp }, …]
}
entry.options = { update_interval, damping_morning, damping_evening, inverter_kw, resolution }
```

`id` plochy je stabilné (generované pri pridaní) – používa sa v `unique_id` entít
a ako kľúč medzipamäte, takže premenovanie plochy nevytvorí nové entity.

## Úložisko (`.storage/forecast_solar_plus.<entry_id>`)

```json
{
  "polled_at": "2026-09-21T11:43:04+00:00",
  "place": "…",
  "ratelimit": { "limit": 12, "remaining": 5, "period": 3600, "reset_at": null, "zone": "IP …" },
  "planes": { "<plane_id>": [["2026-09-21T04:36:51+00:00", 0], …] }
}
```

Ak sa množina `plane_id` nezhoduje s konfiguráciou, medzipamäť sa zahodí. Pri odstránení
záznamu sa súbor zmaže (`async_remove_entry`).

## Rate-limit

`min_update_interval(n_plôch, kľúč) = ceil(60 · n / (limit · 0,8))`; limit 12 (verejný) alebo
60 (kľúč). Možnosti neumožňujú kratší interval; po pridaní plochy sa interval automaticky
zvýši, ak by bol pod minimom. Stav limitu sa číta z tela odpovede (`message.ratelimit`),
hlavičky `X-Ratelimit-*` sú záloha.

## Databáza

Integrácia nemá vlastnú databázu – stavy senzorov ukladá recorder HA štandardne.
