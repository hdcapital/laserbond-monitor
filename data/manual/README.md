# Manual inputs

## lbl_segment_history.csv

**This file ships EMPTY (header only) and must be populated by hand from
actual LaserBond (ASX:LBL) filings** — half-year and annual reports /
Appendix 4D/4E segment notes. It is never populated automatically and the
backtest refuses to run while it is empty. Do not estimate, interpolate or
copy figures from anywhere other than LBL's own published filings.

Columns:

| column | meaning |
|---|---|
| `half` | calendar half, e.g. `2024H1` = Jan–Jun 2024, `2024H2` = Jul–Dec 2024. (LBL's fiscal 1H = calendar `H2` of the prior calendar year.) |
| `services_rev` | Services segment revenue for the half, A$ |
| `products_rev` | Products segment revenue for the half, A$ |
| `technology_rev` | Technology segment revenue for the half, A$ |
| `services_margin_pct` | Services segment margin, % (as disclosed; leave blank if not disclosed) |
| `products_margin_pct` | Products segment margin, % |
| `technology_margin_pct` | Technology segment margin, % |

Leave any undisclosed cell blank — blanks stay blank.

Once populated, run:

```
lbl-tracker backtest
```

which computes Spearman rank correlations and a leave-one-out ridge
regression of each pulse (0/3/6/9/12-month lags) against same-half YoY
segment growth, benchmarked against the naive seasonal base case
(LBL is 2H-skewed, so all comparisons are same-half-vs-same-half).
