# museholdingsum

Muse agent providing investing holding summary.

## Files

- `holdings-summary-prompt.md` — the agent spec: inputs, report layout, classification rules.
- `holdings-summary/build_report.py` — the durable report builder. Pulls live
  holdings via Plaid, computes portfolio views, historical yield, and gross TWR
  from account statement CSVs, and writes a self-contained HTML report.

## Usage

```
python3 holdings-summary/build_report.py <holdings config> <statements CSV> [<transaction CSV> | none]
```

- `<holdings config>` — portfolio types with symbol lists, accounts as
  `<Broker> …<mask>`, and optional `historical annual yield is constant X%` overrides.
- `<statements CSV>` — `Year,Period,Account,Beginning Value,Net Changes,Investment Value Change,Ending Value`
  (`Annual` rows plus optional `Q1`–`Q4` rows).
- Third argument `none` when no transaction-history fee file exists.

Output: `holdings-summary/<prefix>-holdings-summary-<date>.html` — portfolio pie
chart, Gross TWR chart (annual bars + smooth line through annuals and quarterly
year-to-date points), Historical Yield chart (trailing 1-yr at quarter-ends over a
10-year scale, Total emphasized, with the 10-year average line), holdings by
portfolio and by account, and the combined total.

`holdings-summary/price_history.json` is a local cache of dividend-and-split-
adjusted closes; the builder fetches any missing tickers from Yahoo Finance on
first run and caches them.
