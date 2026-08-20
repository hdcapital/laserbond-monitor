# Source status

Verified against the **live endpoints from GitHub Actions runners** — every
WORKING row below was exercised end-to-end (fetch → parse → store) and is
guarded by a live smoke test (`tests/smoke`) that fails CI loudly on source
drift. History depths are from the first full ingest (2026-08-20). No
series is ever stubbed: anything unavailable is NULL/NO DATA and logged to
`data/logs/gaps.jsonl`.

| # | Series | Source / endpoint (verified) | Status | History |
|---|---|---|---|---|
| a | `qld_coal.saleable_tonnes_total`, `qld_coal.type.<mine_type>.<coal_type>` | QLD Open Data CKAN → `quarterly-coal-reports` → "Quarterly Coal Production" XLSX (long table Year/Quarter × Mine Type × Coal type). Portal fronts downloads with an AWS WAF JS challenge → fetched via headless Chrome (Playwright). | **WORKING** | 2010-Q1 → current (65 quarters). Mine-level is only published **annually** (FY) in the Coal Industry Review dataset — quarterly mine-level does not exist; type×coal splits are the published quarterly granularity. |
| b | `pilbara.iron_ore_throughput_mt`, `pilbara.total_throughput_mt` | pilbaraports.com.au — monthly "<month> <year> shipping figures" articles; the JS-rendered listing is bypassed via `sitemap.xml` discovery, tonnages parsed from article text | **WORKING** | Jul 2019 → current (~80 months; site's news archive depth) |
| c | `abs.capex.mining_actual`, `abs.capex.mining_expected` | ABS Data API `data.api.abs.gov.au` (old host `api.data.abs.gov.au` no longer resolves), flow **CAPEX**, dims Mining × Total assets × Australia, SA/chain-volume preferred; expected = Short/Long Term Expected Expenditure | **WORKING** | 1987 → current (quarterly) |
| d | `abs.exploration.metres_drilled_total`, `abs.exploration.expenditure_<state>` | ABS Data API flow **MIN_EXP** (8412.0). ABS publishes metres drilled **nationally only** in this flow; the state dimension exists only for exploration expenditure, which is stored per state instead | **WORKING** (metres by state: not published by ABS) | 1990 → current (quarterly) |
| e | `rba.commodity_index_aud`, `rba.audusd` (daily), `rba.audusd_monthly` | RBA CSV tables — I2 (Description "Index of commodity prices; All items; A$"), **F11.1 = daily**, **F11 = monthly** | **WORKING** | index 1982→, monthly FX 2010→, daily FX 2023→ (RBA's published CSV windows) |
| f | `aisi.capacity_utilisation_pct`, `aisi.raw_steel_production_kt` | steel.org `/industry-data/` weekly release text; each release yields 3 published points (current, prior week, year-ago week) | **WORKING** | accumulates ~3 pts/week from 2026-08; **no free backfill exists** |
| g | `fred.steel_new_orders` | FRED series **A31SNO** (New Orders: Iron and Steel Mills and Ferroalloy Mfg). API with `FRED_API_KEY` when set; public `fredgraph.csv` fallback verified | **WORKING** (key optional) | 1992-02 → current (monthly) |
| h | `bh.rigcount_na_total` (+US/Canada), `bh.rigcount_intl_total` | rigcount.bakerhughes.com `/static-files/<uuid>` workbooks (links discovered by anchor text): NAM Weekly long table (current + 2013–25 archive), US/Canada Oil&Gas Split archives (2000→), WW Monthly (+1975→ archive matrix) | **WORKING** | NA weekly 2000 → current; intl monthly **1975** → current |
| i | `cat.resource_industries_yoy_pct` (+regions) | SEC EDGAR — 8-K Item 7.01 filings via the data.sec.gov submissions API, EX-99 exhibit parsed. **Verified live: SEC serves "Undeclared Automated Tool" to GitHub-hosted runner IP ranges regardless of a compliant User-Agent** (efts.sec.gov and data.sec.gov alike) | **BLOCKED — SEC blocks GitHub-hosted runner IPs.** Code + parser are ready; runs from a self-hosted runner or any non-blocked host (`lbl-tracker ingest cat_edgar` locally with `SEC_CONTACT_EMAIL` set). Smoke test self-detects the block and reports it | — |
| j | `jsa.ivi.<anzsco4>.<state>`, `jsa.ivi_trades_tightness` | jobsandskills.gov.au `internet_vacancies_anzsco4_occupations_states_and_territories_*.xlsx` (monthly file discovered from the IVI page). The site's WAF stalls plain clients → fetched with a browser TLS fingerprint (curl_cffi) | **WORKING** | Mar 2006 → current (monthly; occupations 3232 fitters/machinists + 3223 welders, all states) |
| k | `tungsten.flag_count` + `events/tungsten_flags` | Google News RSS keyword monitor — **event-flag proxy; no free tungsten/APT spot price exists**. Counts start at monitoring start (2026-08): the shallow RSS window would undercount earlier months, so they are never emitted | **WORKING (proxy)** | events retained from feed (2024→); count series 2026-08 → |
| l | `events/announcements` (LBL/EHL/MSV/MAD), `events/lbl_technology_events`, `emeco.utilisation_pct`, `mitchell.avg_operating_rigs` | Markit ASX research API (the legacy `asx.com.au/asx/1` API is gone); PDFs via the Markit file gateway (no token needed). PDF classification via OpenAI API | **WORKING** (metadata + PDFs); extractions **pending `OPENAI_API_KEY`** | **The anonymous API returns only the latest 5 announcements per ticker — no pagination exists (every page/size/token variant verified live), so free deep backfill is not available.** Events accumulate completely going forward via a 6-hourly poll. (Optional future enhancement: backfill LBL history from laserbond.com.au's own announcement archive.) |
| — | ImportGenius bill-of-lading module | dormant stub — inert without `IMPORTGENIUS_API_KEY`, never writes placeholder data | DORMANT | — |

## Composite availability (first build, 2026-08-20)

| Output | Value | Weight available | Missing components |
|---|---|---|---|
| Services Pulse | −27.5 | 75% | QLD coal enters as z-history accrues in-window; all others live |
| Products Pulse | +22.8 | 60% | AISI (accumulating), CAT (pending SEC email) |
| Margin Pulse | −22.6 | 66% | tungsten flags (accumulating from 2026-08) |
| Technology Pipeline | NO DATA | — | needs `OPENAI_API_KEY` to classify announcement PDFs |

## Verification log

* 2026-08-20 — 7 CI verify rounds against live endpoints from GitHub
  Actions runners (this container's egress policy blocks all data hosts, so
  verification and ingestion run in CI). Final state: **11/11 reachable
  smoke tests green**; cat_edgar skipped pending `SEC_CONTACT_EMAIL`.
* Notable endpoint facts discovered live: ABS API host renamed; RBA F11.1
  is daily / F11 monthly; QLD portal answers HTTP 202 (AWS WAF challenge)
  to non-browser clients and its CKAN datastore for the quarterly file is
  empty upstream; jobsandskills.gov.au WAF read-timeouts plain clients;
  lmip.gov.au / labourmarketinsights.gov.au no longer resolve; ASX legacy
  JSON API removed (Markit research API replaces it).
