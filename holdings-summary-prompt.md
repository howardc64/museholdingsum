# Holdings Summary — Agent Prompt

**Purpose:** Build a readable holdings summary for any set of brokerage accounts, pulling live positions from Plaid. Portfolio types, account numbers, and symbol mappings all come from the input file — nothing is hardcoded here.

## How to run this

The agent requires two inputs, with an optional third:

1. A **holdings config file** — portfolio types, accounts, and yield overrides (e.g. `HN holdings.txt`).
2. A **statements CSV file** — the account statements file with Year, Period, Account, Beginning Value, Net Changes, Investment Value Change, and Ending Value per account per period (e.g. `NH stmts.csv`). Period is `Annual` for yearly rows and `Q1`–`Q4` for quarterly rows. Used for the Gross TWR section (annual rows only).
3. A **transaction history CSV** (optional) — deducted advisory/alternative-investment fees, added back so returns are gross of fees. Pass `none` when no fee file exists for these accounts; the report then notes fee data is unavailable and computes TWR without a fee add-back.

Howard launches it like:

> Run the holdings summary against `<holdings config>` and `<statements CSV>` [`<transaction history CSV>` | `none`].

Without the holdings config and statements CSV, stop and ask for the missing one. The agent needs nothing else.

## Holdings config file format

A plain-text file with two sections:

```
== Portfolio Types

<Portfolio Name>
	<indented lines: explicit symbols and/or classification hints>

== Accounts

<Broker Name>
	...<account mask digits>
	...<account mask digits>
```

- Portfolio names are free-form (whatever the config says).
- Under each portfolio, indented lines are either explicit symbols (e.g. `09599L846`) that belong to that portfolio, or plain-language classification hints (e.g. "symbols with low yield targeting capital preservation").
- Under `== Accounts`, each indented line is an account mask like `...4801` — match by the trailing digits.
- Multiple brokers are allowed; each broker heading is followed by its own account masks.
- An optional `== Historical Yield Performance` section holds per-symbol yield overrides, one per line: `<SYMBOL> historical annual yield is constant <X>% / year`. A symbol with an override uses that constant annualized return for every trailing period in the Holdings Historical Yield table instead of market price history (for YTD, the constant is prorated by the fraction of the year elapsed).

## Data source (Plaid)

Read `/opt/hatch/skills/plaid/SKILL.md` first and follow it. Read-only task.

1. Run `plaid accounts` to map `account_id` → masked number and account name.
2. Run `plaid investments-holdings` for positions: each holding carries `account_id`, `quantity`, `institution_price`, `institution_value`, and usually `cost_basis`; `securities[]` maps `security_id` → `ticker_symbol`, `name`, and `type`. Note the `institution_price_as_of` date for the report header.
3. Keep only holdings whose account mask matches one of the config's account masks. Skip pseudo-holdings with ticker `CUR:USD` (cash-balance placeholders).
4. Plaid data may lag the live account — say so in the report if prices look stale.

## Portfolio classification

For each position's ticker, in order:

1. **Explicit symbol match** — if the ticker appears under a portfolio in the config, it belongs to that portfolio. Match against the Plaid `ticker_symbol` first, then against the security `name` and any identifier fields (`cusip`, `isin`, `sedol`), since configs sometimes list a CUSIP where Plaid reports a ticker.
2. **New symbol proposal** — if the ticker is not in the config, research what it is: start from the Plaid security `name` and `type`; if the name is ambiguous, look up the fund's category/objective on the web (e.g. Morningstar category). Then propose a portfolio with one line of reasoning, guided by the config's classification hints (capital preservation = low-yield/cash-like; high income = higher-yielding, no equities; global equity = equities and real estate). List each proposal in the report under **"Proposed classifications — needs your approval"** with the reasoning. The position still appears under "Unclassified" in this run's numbers.
3. **Config update only on approval** — never write a new symbol into the config file without Howard's explicit yes. When he approves a proposal, append the symbol to that portfolio's symbol list in the config (keeping the file's existing format) and note the update.
4. **Unmatched config symbols** — if a symbol listed in the config matches no holding at all, flag it by name in the report (it may be listed under an identifier Plaid doesn't report).

## Per-position math

- **Market value** = `institution_value`. **Price** = `institution_price`. **Shares** = `quantity`.
- **Cost basis** = `cost_basis` when present; for cash/money-market positions where it is null, use market value (gain/loss $0).
- **Unrealized gain/loss** = market value − cost basis.
- **% of account** = position market value ÷ account total market value. **% of portfolio** = account's portfolio market value ÷ portfolio total.
- Plaid aggregates by security per account (no tax lots) — one row per ticker per account.

## Output format

Build a **self-contained HTML file** named `holdings-summary-<YYYY-MM-DD>.html` (today's date), saved next to the config file unless told otherwise. Content and order:

### 1. Title and source note

`Holdings Summary — As of <institution_price_as_of date>` plus a note: live Plaid data, prices as of the stated date, and which accounts were included (broker + mask).

### 2. Portfolio view (first)

Heading `Holdings by Portfolio`. Three charts side by side at the top, then one table per portfolio (in config order):

| Account | Market value | % of portfolio | Cost basis | Unrealized G/L |

- Chart 1: portfolio pie chart. Chart 2: Gross TWR — annual bars at each year-end with a smooth (monotone-cubic) line running through the annuals and continuing through the cumulative year-to-date quarterly points, on a true calendar date axis. Chart 3: Historical Yield — 10-year x scale; smooth lines of the trailing 1-year total return at each quarter-end, one thin muted line per portfolio with the Total line drawn thick and dark on top, same date-axis treatment. A dashed horizontal line marks the Total's trailing 10-year annualized average, labeled with its % value at the right end. No data-point markers on the lines; each line starts with a mirrored ghost sample (flat clamped start) so it never implies enormous prior data. Major x-axis ticks fall on Dec 31 labeled with that year, so an annual point always sits on its own year's tick. A caption under the charts explains the quarterly points.

- One row per account holding the portfolio, in config account order. The Account column uses the same `<Broker> …<mask>` format.
- The portfolio total is the **last row of the table** (a SUM row labeled "Total").

### 2b. Holdings Historical Yield (after the portfolio view)

Heading `Holdings Historical Yield`. One table, one row per portfolio (same order as the portfolio view), with exactly these columns:

| Portfolio | YTD | 1-year | 3-year | 5-year | 10-year |

YTD is the simple (non-annualized) return from the last trading day of the prior year through the as-of date. The 1/3/5/10-year cells are the portfolio's **annualized average total return** over the trailing period, as a percentage with 1 decimal. Method:

1. For each holding with usable market price history, take dividend-and-split-adjusted closes and compute the return: for YTD, the simple return from the last trading day of the prior year through the as-of date; for the trailing 1/3/5/10-year periods, the annualized total return `(P_end / P_start)^(365.25 / days) − 1`, measured back from the report's as-of date.
2. The portfolio figure is the **market-value-weighted average** of its holdings' returns, with weights renormalized over the holdings that have data for that period.
2b. The table ends with a **Total** row: the market-value-weighted average across all holdings with data for each period (i.e. the combined portfolio's annualized return).
3. Holdings with no usable price history (money-market funds, non-traded securities) are **excluded** and footnoted by name with their dollar amounts — **unless** the config's `== Historical Yield Performance` section gives a constant annual yield for the symbol, in which case use that constant for every period. A holding launched too recently for a period shows as "n/a" for that period and is excluded from that period's average.
4. Below the table, footnote: the data source, the as-of date, which positions were excluded, and that historical returns are backward-looking and not predictive of future results.

### 2c. Gross TWR (after Holdings Historical Yield)

Heading `Gross TWR`. One table for the combined portfolio (all accounts in the config), with exactly these rows:

|  | 2026-Q4 | 2026-Q3 | 2026-Q2 | 2026-Q1 | 2025 | 2024 | … |
|---|---|---|---|---|---|---|---|
| Start Value |  | $ | $ | $ | $ | $ | $ |
| Net Flows |  | $ | $ | $ | $ | $ | $ |
| Investment Change |  | $ | $ | $ | $ | $ | $ |
| End Value |  | $ | $ | $ | $ | $ | $ |
| Gross TWR |  | % | % | % | % | % | % |

Column rules:
- Annual columns: the 5 completed years before the quarterly year (or the 5 most recent annual years if the CSV has no quarterly rows), newest year on the left. No older years appear.
- Quarterly columns: for the latest year that has quarterly rows (`Q1`–`Q4`), one column per quarter, newest quarter on the left (Q4, Q3, Q2, Q1), placed before the annual columns. Each quarterly column is cumulative year-to-date from Jan 1 through that quarter end: Start Value = Jan 1 portfolio value; Net Flows = Σ Net Changes for Q1 through that quarter; End Value = Σ Ending Value at that quarter end; Investment Change = End − Start − Net Flows; Gross TWR = (Investment Change + fees deducted through that quarter) ÷ (Start Value + ½ × Net Flows). Quarters that have not ended yet stay blank. Quarterly columns are shown only when every account in the annual table has at least one quarterly row for that year — otherwise a "combined" quarterly column would silently exclude accounts, so they are omitted.
- If an account is missing a quarterly row (e.g. no Q1 statement), begin it at its prior year-end value and end it at its next known quarter boundary, with no flow split for the missing quarter.
- The report filename and title follow the holdings config: `HN holdings.txt` → `hn-holdings-summary-<date>.html` ("HN holdings Summary"); `GJ-holdings.txt` → `gj-idgt-holdings-summary-<date>.html` ("GJ-IDGT holdings Summary"); `Lily holdings.txt` → `lily-holdings-summary-<date>.html` ("Lily holdings Summary"). Distinct prefixes keep reports from overwriting each other.

Method:
1. Source: the account statements CSV given as the second input: Year, Period, Account, Beginning Value, Net Changes, Investment Value Change, Ending Value. Use only rows with Period `Annual` for this section.
2. Combine across the config's accounts per year: Start Value = Σ Beginning Value; Net Flows = Σ Net Changes; Investment Change = Σ Investment Value Change; End Value = Σ Ending Value.
3. Gross TWR per year = (Σ Investment Value Change + Fees) ÷ (Σ Beginning Value + ½ × Σ Net Changes), where Fees is that year's deducted advisory/alternative-investment fees from the transaction history CSV when one is provided (added back so the return is gross of fees; 0 when no fee file exists). Flows are assumed mid-year. Percentage with 1 decimal.
4. Footnote the sources and the mid-year flow assumption.
5. Below the table, a line reading `Gross TWR annual averages — 1yr avg: %, 3yr avg: %, 5yr avg: %`, where each is the geometric mean of the newest 1, 3, and 5 annual TWRs (annualized).

### 3. Per-account view (second)

One section per account (config order). Each section heading is the brokerage firm name from the config followed by the account mask: `<Broker> …<mask>` (e.g. `Fidelity …4801`). Below the heading, a total line (`$X market value · $X cost basis · $X unrealized gain/loss`), then one table per portfolio:

| Symbol | Description | Shares | Price | Market value | % of acct | Cost basis | Unrealized G/L |

### 4. Combined total

Final `Combined total` line with summed market value, cost basis, and unrealized gain/loss.

### Formatting rules

- Money: `$` with thousands commas, **whole dollars — no cents**. Percentages: 1 decimal.
- Clean, readable styling: system font, bordered tables, right-aligned numbers.
- Every number must come from Plaid. Never invent, estimate, or carry over prices.

## Pie chart

- One slice per portfolio (market value, whole dollars), labeled with value and percentage of the combined total. Title: `Holdings by Portfolio — $<combined total> total`.
- Use matplotlib (`matplotlib.use("Agg")`), white bold slice labels, a legend. Embed in the HTML as a base64 data URI (`<img src="data:image/png;base64,...">`) so the file is self-contained.

## Guardrails

- Read-only: never move money or change anything. This is a reporting task, not financial advice.
- If a holding's cost basis is null and it is not a cash/money-market position, show cost basis as "—" and exclude it from the unrealized math, with a footnote.
- If Plaid returns no holdings for a listed account, say so explicitly instead of omitting it.
- If a ticker could not be classified into any portfolio, flag it by name — do not silently drop it.
