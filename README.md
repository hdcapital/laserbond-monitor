# lbl-tracker

External-data nowcast for **LaserBond (ASX:LBL)** — a surface-engineering
company with three segments:

* **Services** — mining maintenance/refurb, driven by physical mining
  activity and deferred-maintenance cycles
* **Products** — OEM wear components and steel-mill rolls, export-heavy,
  tungsten-carbide input costs
* **Technology** — lumpy licensing deals; event-driven, not macro-driven

Live source status, history depth and verified endpoints: **[SOURCES.md](SOURCES.md)**.

## Data integrity rules (non-negotiable)

1. **Nothing is ever fabricated, estimated, interpolated or backfilled.**
   If a source fails or lacks history the store keeps NULL and the gap is
   logged (`data/logs/gaps.jsonl`). Every row carries
   `series_id, date, value, source_url, retrieved_at`.
2. Ingesters were written against live responses; a source that stops
   matching reality turns the smoke-test job red (**CI fails loudly on
   source drift**) and is marked BLOCKED in SOURCES.md rather than stubbed.
3. No demo/seed/example numeric data anywhere in the repo. Dashboards
   render **NO DATA** for missing series. (Unit tests use obviously
   synthetic in-memory fixtures that never touch the store.)

## Layout

```
config.yaml                 weights / tickers / keywords - edit here
src/lbl_tracker/ingest/     one idempotent ingester per source
src/lbl_tracker/analytics/  pulse composites + backtest harness
src/lbl_tracker/dashboard/  static HTML builder -> /docs (GitHub Pages)
data/parquet/observations/  the store (parquet, committed by CI)
data/parquet/events/        announcements, extractions, news flags
data/manual/                lbl_segment_history.csv - EMPTY, see its README
docs/                       dashboard (GitHub Pages)
tests/smoke/                live smoke tests, one per source
```

## Outputs

Four monthly outputs (weights documented + editable in `config.yaml`):

| Output | Composition |
|---|---|
| **Services Pulse** | 50% physical activity (QLD coal + Pilbara Ports) + 25% ABS mining capex incl. expectations + 15% RBA commodity index + 10% ABS exploration metres drilled |
| **Products Pulse** | AISI utilisation + US steel new orders + CAT Resource Industries + Baker Hughes rigs + inverted AUD |
| **Margin Pulse** | tungsten policy flags (proxy) + fitter/welder vacancy tightness + AUD — all inverted (higher pulse = margin tailwind) |
| **Technology Pipeline** | event table from classified LBL announcements: contracted-unrecognised $, stage counts, 6-month deltas — **facts, never scored** |

Pulses are −100…+100 composites of 5-year rolling z-scores (window,
minimum history and clipping in `config.yaml → zscore`). A component
enters a month only if its latest observation is within its
`max_stale_months` (frequency alignment; the store itself is never
forward-filled). Weights renormalise over available components; below 50%
available weight the pulse reports NO DATA.

## Running

```bash
pip install -e ".[dev]"
cp .env.example .env       # fill in secrets

lbl-tracker probe          # live endpoint diagnostics (no writes)
lbl-tracker ingest         # all sources, idempotent
lbl-tracker pulses         # composites -> data/parquet/pulses.parquet + docs/data
lbl-tracker dashboard      # static dashboard -> docs/index.html
lbl-tracker backtest       # needs data/manual/lbl_segment_history.csv populated
lbl-tracker status         # per-series freshness
lbl-tracker duckdb         # rebuild DuckDB views over the parquet store
lbl-tracker email          # monthly brief (SMTP secrets)

pytest tests/unit          # fast, no network
pytest -m live tests/smoke # live source-drift gate
```

## Automation (GitHub Actions)

| Workflow | Schedule | Does |
|---|---|---|
| `verify` | on push + manual | unit tests, live probes, live smoke tests |
| `ingest-weekly` | Mon 20:30 UTC | full ingest → pulses → dashboard → commit; then smoke gate |
| `announcements-daily` | every 6h | ASX announcements + tungsten flags → dashboard → commit |
| `email-monthly` | 1st, 21:00 UTC | monthly SMTP brief |

Cron triggers run on the **default branch**; enable GitHub Pages
(Settings → Pages → deploy from branch, `/docs`) to publish the dashboard.

### Secrets (Actions repository secrets; also `.env` locally)

| Secret | Needed for |
|---|---|
| `FRED_API_KEY` | US steel new orders via the FRED API (public CSV fallback works without it) |
| `OPENAI_API_KEY` | announcement PDF classification (Emeco/Mitchell/Mader/LBL Technology events); model set by repo variable `OPENAI_MODEL` (default `gpt-5-mini` in config.yaml) |
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/TO` | monthly email brief. Gmail: host `smtp.gmail.com`, port `587`, user/from = the Gmail address, password = a 16-char [App Password](https://myaccount.google.com/apppasswords) (needs 2-Step Verification; regular passwords won't work) |
| `SEC_CONTACT_EMAIL` | polite EDGAR User-Agent (optional) |
| `IMPORTGENIUS_API_KEY` | optional bill-of-lading module (dormant without it) |

## Backtest

`data/manual/lbl_segment_history.csv` ships **empty** and must be filled
from actual LBL filings (see `data/manual/README.md`). Once populated,
`lbl-tracker backtest` reports Spearman rank correlations and
leave-one-out ridge regression of each pulse (0/3/6/9/12-month lags)
against same-half YoY segment growth, benchmarked against the naive
seasonal base case (LBL is 2H-skewed).
