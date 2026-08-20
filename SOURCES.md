# Source status

Status of every ingested series. **VERIFYING** means the ingester is
written but the live endpoint has not yet been confirmed from CI (this
build environment has no egress to data hosts; verification runs on
GitHub Actions). This table is updated from real CI runs — a source that
cannot be made to work is marked **BLOCKED** with the reason, never
stubbed with sample data.

| # | Series | Source / endpoint | Status | History depth |
|---|---|---|---|---|
| a | `qld_coal.saleable_tonnes_total` (+ per-mine) | QLD Open Data CKAN API → coal industry review XLSX/CSV | VERIFYING | — |
| b | `pilbara.iron_ore_throughput_mt`, `pilbara.total_throughput_mt` | Pilbara Ports monthly media statements (scrape) | VERIFYING | — |
| c | `abs.capex.mining_actual`, `abs.capex.mining_expected` | ABS Data API (SDMX-JSON), flow discovered from /dataflow | VERIFYING | — |
| d | `abs.exploration.metres_drilled_*` | ABS Data API (SDMX-JSON), 8412.0 | VERIFYING | — |
| e | `rba.commodity_index_aud`, `rba.audusd`, `rba.audusd_monthly` | RBA statistical tables CSV (I2, F11.1, daily FX) | VERIFYING | — |
| f | `aisi.capacity_utilisation_pct`, `aisi.raw_steel_production_kt` | AISI weekly release (scrape, steel.org) | VERIFYING | accumulates weekly — no free backfill |
| g | `fred.steel_new_orders` | FRED API (needs FRED_API_KEY; public CSV fallback) | VERIFYING | — |
| h | `bh.rigcount_na_total`, `bh.rigcount_intl_total` (+US/Canada) | Baker Hughes published XLSX (links discovered) | VERIFYING | — |
| i | `cat.resource_industries_yoy_pct` (+regions) | SEC EDGAR full-text search → 8-K exhibit parse | VERIFYING | — |
| j | `jsa.ivi.*`, `jsa.ivi_trades_tightness` | Jobs & Skills Australia IVI detailed-occupation XLSX | VERIFYING | — |
| k | `tungsten.flag_count` + `events/tungsten_flags` | Google News RSS keyword monitor — **event-flag proxy, no free spot price exists** | VERIFYING | accumulates from first ingest — RSS window is shallow, never backfilled |
| l | `events/announcements`, `events/lbl_technology_events`, `emeco.utilisation_pct`, `mitchell.avg_operating_rigs` | ASX public announcements JSON + PDF classification via Anthropic API (needs ANTHROPIC_API_KEY) | VERIFYING | — |
| — | ImportGenius bill-of-lading module | dormant stub — inert without IMPORTGENIUS_API_KEY | DORMANT | — |

## Verification log

* (pending first CI verify run)
