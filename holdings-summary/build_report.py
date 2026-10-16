#!/usr/bin/env python3
"""Holdings Summary report builder — implements holdings-summary-prompt.md.
Reads the holdings config, pulls live Plaid positions, and writes the
self-contained HTML report. Run: python3 build_report.py [config_path] [summary_csv_path]
"""
import csv, json, re, base64, io, html, subprocess, sys, os
from datetime import datetime, date, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/workspace/user/files/HN holdings.txt')
SUM_CSV_ARG = sys.argv[2] if len(sys.argv) > 2 else None
HIST_CSV = os.path.join(os.path.dirname(CONFIG), 'Accounts_History_Merged.csv')
PRICE_CACHE = os.path.join(HERE, 'price_history.json')
OUTDIR = HERE
STAMP = date.today().isoformat()
_CFG_BASE = os.path.basename(CONFIG)
_CFG_NAME = re.sub(r'\.[^.]+$', '', _CFG_BASE).strip()
_CFG_SLUG = re.sub(r'\s+', '-', _CFG_NAME).lower()
_OUT_SLUG = re.sub(r'-holdings$', '', _CFG_SLUG)
# report filename prefix follows the config: HN holdings -> hn-holdings-...,
# GJ-holdings -> gj-idgt-holdings-... (legacy RLT configs keep the bare name)
_PREFIX_MAP = {
    'hn-holdings': ('hn-', 'HN holdings'),
    'gj-holdings': ('gj-idgt-', 'GJ-IDGT holdings'),
    'lily-holdings': ('lily-', 'Lily holdings'),
    'lily_holdings': ('lily-', 'Lily holdings'),
}
_OUT_DEFAULT = _OUT_SLUG + '-' if _OUT_SLUG and not _CFG_SLUG.startswith('rlt') else ''
OUT_PREFIX, REPORT_TITLE = _PREFIX_MAP.get(
    _CFG_SLUG, (_OUT_DEFAULT, _CFG_NAME if _CFG_SLUG else 'Holdings'))
END = date.today()

E = html.escape
def money(x): return f"${x:,.0f}"
def pct(x): return f"{x*100:.1f}%"
def num(x):
    try: return float(str(x).replace(',', ''))
    except: return 0.0
def pd(d): return datetime.strptime(d.strip(), '%m/%d/%Y').date()

# ---------------- config ----------------
portfolios, accounts, yield_overrides = [], [], {}
us_splits = {}
section = None
in_hy = False
for line in open(CONFIG):
    s = line.rstrip('\n')
    if s.startswith('=='):
        sec = s.strip('= ').strip()
        section = sec
        in_hy = (sec == 'Historical Yield Performance')
        continue
    if not s.strip(): continue
    if in_hy:
        m = re.match(r'(.+?)\s+historical annual yield is constant\s+([0-9.]+)%', s.strip(), re.I)
        if m: yield_overrides[m.group(1).strip()] = float(m.group(2)) / 100.0
        continue
    if section == 'US/Foreign Splits':
        m = re.match(r'([A-Z0-9.\-]{2,})\s+([0-9.]+)%\s*US\b', s.strip(), re.I)
        if m: us_splits[m.group(1).upper()] = float(m.group(2)) / 100.0
        continue
    if section == 'Portfolio Types':
        if not s.startswith(('\t', ' ')):
            portfolios.append({'name': s.strip(), 'symbols': [], 'hints': []})
        else:
            txt = s.strip()
            key = 'symbols' if re.fullmatch(r'[A-Z0-9.\-]{2,}', txt) else 'hints'
            portfolios[-1][key].append(txt)
    elif section == 'Accounts':
        if not s.startswith(('\t', ' ')):
            cur_broker = s.strip()
        else:
            accounts.append({'broker': cur_broker, 'mask': re.sub(r'\D', '', s)})
masks = {a['mask'] for a in accounts}
brokers = sorted(set(a['broker'] for a in accounts))
def acct_label(m):
    b = next((a['broker'] for a in accounts if a['mask'] == m), '')
    return f"{b} …{m}" if b else f"…{m}"

# ---------------- Plaid positions ----------------
def plaid(cmd):
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    raw = out.stdout
    return json.loads(raw[raw.find('{'):])

body = plaid('plaid investments-holdings')['body']
holds, secs = body['holdings'], {s['security_id']: s for s in body['securities']}
accmap = {a['account_id']: a for a in body['accounts']}
asof = None
positions = []
for h in holds:
    a = accmap.get(h['account_id'], {})
    mask = str(a.get('mask') or '')
    if mask not in masks: continue
    sec = secs.get(h['security_id'], {})
    t = sec.get('ticker_symbol') or ''
    if t == 'CUR:USD': continue
    if not asof: asof = h.get('institution_price_as_of')
    qty = h.get('quantity') or 0; price = h.get('institution_price') or 0
    val = h.get('institution_value') or 0
    cb = h.get('cost_basis')
    if cb is None: cb = val
    positions.append({'mask': mask, 'ticker': t, 'desc': sec.get('name') or '',
                      'stype': sec.get('type') or '',
                      'qty': qty, 'price': price, 'val': val, 'cb': cb, 'gl': val - cb})
asof_d = date.fromisoformat(str(asof)) if asof else END

# classify into portfolios
def canon(t):
    return 'REIT' if t == 'BLUEPRINT.CAPITAL.INCOME.REIT' else t
def _ovr_rate(t):
    # constant-yield override for t, matching raw, canon, or canon-of-key forms
    if t in yield_overrides: return yield_overrides[t]
    c = canon(t)
    if c in yield_overrides: return yield_overrides[c]
    for k, v in yield_overrides.items():
        if canon(k) == t or canon(k) == c:
            return v
    return None
def classify(ticker, sec_name=''):
    ids = [ticker, sec_name]
    for p in portfolios:
        if any(sym and any(sym in str(x) for x in ids) for sym in p['symbols']):
            return p['name']
    return None
unmapped = {}
for r in positions:
    p = classify(r['ticker'], r['desc'])
    if p is None:
        p = 'Unclassified'
        unmapped[r['ticker']] = (r['desc'], r['stype'], r['val'])
    r['port'] = p
port_names = [p['name'] for p in portfolios] + (['Unclassified'] if unmapped else [])

# aggregates
port_total = defaultdict(float); acct_total = defaultdict(float)
acct_cb = defaultdict(float); acct_gl = defaultdict(float)
for r in positions:
    port_total[r['port']] += r['val']
    acct_total[r['mask']] += r['val']
    acct_cb[r['mask']] += r['cb']; acct_gl[r['mask']] += r['gl']
grand = sum(port_total.values())
acct_order = [a['mask'] for a in accounts]

# ---------------- price history (Yahoo, cached) ----------------
need = set()
for r in positions: need.add(r['ticker'])
need |= {'VWIUX', 'FSTFX'}
need -= {'BLUEPRINT.CAPITAL.INCOME.REIT', 'CUR:USD', 'FZFXX', 'FDRXX'}
hist = {}
if os.path.exists(PRICE_CACHE):
    hist = json.load(open(PRICE_CACHE))
missing = [t for t in need if t not in hist]
if missing:
    import urllib.request, time
    end_u = int(datetime(END.year, END.month, END.day).timestamp())
    start_u = int(datetime(2023, 1, 1).timestamp())
    for t in missing:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?period1={start_u}&period2={end_u}&interval=1d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            rr = d["chart"]["result"][0]
            ts, adj = rr["timestamp"], rr["indicators"]["adjclose"][0]["adjclose"]
            pts = sorted((datetime.fromtimestamp(x, timezone.utc).date(), a)
                         for x, a in zip(ts, adj) if a)
            hist[t] = [(str(dd), p) for dd, p in pts]
        except Exception as e:
            print(f'price fetch failed for {t}: {e}', file=sys.stderr)
        time.sleep(0.3)
    json.dump(hist, open(PRICE_CACHE, 'w'))

def price_at(ticker, d):
    c = canon(ticker)
    if c in ('FZFXX', 'FDRXX', 'FCASH', 'MTN'): return 1.0
    if c == 'REIT':
        yrs = (asof_d - d).days / 365.25
        pn = val_now_reit_price()
        return pn / (1.08 ** yrs)
    pts = hist.get(ticker)
    if not pts: return None
    best = None
    for ds, p in pts:
        dd = date.fromisoformat(ds)
        if dd <= d: best = p
        else: break
    return best

def val_now_reit_price():
    v = sum(r['val'] for r in positions if canon(r['ticker']) == 'REIT')
    q = sum(r['qty'] for r in positions if canon(r['ticker']) == 'REIT')
    return v / q if q else 0

# ---------------- allocation donut chart (portfolio view) ----------------
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import math
def _donut_b64(vals, labels, total, title=None):
    # allocation donut in the report's style: ring with total value centered,
    # Category / Current (%) legend table on the right
    _palette = ['#6ba3be', '#a8bfa0', '#b0a191', '#b9a7c9', '#8fb8b0', '#c9a86a']
    _dcolors = [_palette[i % len(_palette)] for i in range(len(vals))]
    _fig0, (_axd, _axl) = plt.subplots(1, 2, figsize=(10, 4.5), gridspec_kw={'width_ratios': [1, 1.15]})
    if title:
        _fig0.suptitle(title, fontsize=13, weight='bold')
    _axd.pie(vals, colors=_dcolors, startangle=90, counterclock=False,
             wedgeprops=dict(width=0.42, edgecolor='white', linewidth=2))
    _axd.text(0, 0.10, 'TOTAL VALUE', ha='center', va='center', fontsize=11, color='#777')
    _axd.text(0, -0.14, f"${total:,.0f}", ha='center', va='center', fontsize=22, weight='bold')
    _axd.axis('equal')
    _axl.axis('off')
    _axl.text(0.0, 0.92, 'Category', fontsize=11, color='#777', transform=_axl.transAxes, va='center')
    _axl.text(1.0, 0.92, 'Current (%)', fontsize=11, color='#777', transform=_axl.transAxes, va='center', ha='right')
    for _i, (_lb, _v) in enumerate(zip(labels, vals)):
        _y = 0.78 - _i * 0.17
        _axl.plot(0.035, _y, 'o', color=_dcolors[_i], markersize=11, transform=_axl.transAxes, clip_on=False)
        _axl.text(0.09, _y, _lb, fontsize=12, weight='bold', transform=_axl.transAxes, va='center')
        _axl.text(0.99, _y, f"{_v/total*100:.2f}%", fontsize=12, transform=_axl.transAxes, va='center', ha='right')
        _axl.plot([0, 1], [_y - 0.085, _y - 0.085], color='#e0e0e0', linewidth=0.8,
                  transform=_axl.transAxes, clip_on=False)
    _buf0 = io.BytesIO(); _fig0.savefig(_buf0, format='png', bbox_inches='tight'); plt.close(_fig0)
    return base64.b64encode(_buf0.getvalue()).decode()

vals = [port_total[p] for p in port_names if port_total[p] > 0]
labels = [p for p in port_names if port_total[p] > 0]
_pairs = sorted(zip(vals, labels), key=lambda t: -t[0])
vals = [v for v, _ in _pairs]
labels = [l for _, l in _pairs]
chart_b64 = _donut_b64(vals, labels, grand)
# second donut: US vs foreign dollars inside the split-covered (global) holdings
_usd = sum(r['val'] * us_splits[r['ticker']] for r in positions if r['ticker'] in us_splits)
_frd = sum(r['val'] * (1 - us_splits[r['ticker']]) for r in positions if r['ticker'] in us_splits)
us_foreign_b64 = _donut_b64([_usd, _frd], ['US', 'Foreign'], _usd + _frd,
                            title="Global Holdings — US vs Foreign") if _usd + _frd > 0 else ""

# ---------------- smooth time-series line chart helper ----------------
import matplotlib.dates as mdates
import numpy as _np
from scipy.interpolate import PchipInterpolator
def _line_chart(series, title, ylabel, legend=True, bars=None, emphasis=None, hlines=None, ylim=None, figsize=(5.6, 4.2)):
    # series: [(label, [(date, frac), ...])]; monotone-cubic (Pchip) smooth curve
    # on a true date axis — no overshoot between points, x spacing is calendar time.
    # A mirrored ghost sample (same value, one typical interval before the first
    # point) clamps the start flat so the curve never implies enormous prior data.
    # `bars`: [(date, frac)] drawn behind the line. `emphasis`: {label: color}
    # drawn thick and opaque on top, in dict order; everything else goes thin and
    # muted. A plain string is treated as {label: '#1a1a1a'}. `hlines`: [(frac,
    # label)] horizontal dashed reference lines with a % label at the right end.
    # `ylim`: fixed y-axis range (percent units), used to share a scale across charts.
    fig, ax = plt.subplots(figsize=figsize, constrained_layout=True)
    if ylim:
        ax.set_ylim(ylim)
    _emph = emphasis if isinstance(emphasis, dict) else ({emphasis: '#1a1a1a'} if emphasis else {})
    _by_label = {lb: sorted(pts) for lb, pts in series if pts}
    _ordered = [(lb, _by_label[lb]) for lb in _by_label if lb not in _emph]
    for lb in _emph:
        if lb in _by_label:
            _ordered.append((lb, _by_label[lb]))
    _x_last = None
    for label, pts in _ordered:
        ds = [d for d, _ in pts]
        ys = [v * 100 for _, v in pts]
        xs = list(mdates.date2num(ds))
        _is_emph = label in _emph
        _lw = 3.5 if _is_emph else (2.2 if not _emph else 1.2)
        _al = 1.0 if (_is_emph or not _emph) else 0.6
        _col = _emph.get(label) if _is_emph else None
        if len(pts) >= 2:
            _dxs = sorted(xs[i + 1] - xs[i] for i in range(len(xs) - 1))
            _dx = _dxs[len(_dxs) // 2]
            _spl = PchipInterpolator([xs[0] - _dx] + xs, [ys[0]] + ys)
            _xn = _np.linspace(xs[0], xs[-1], 400)
            ax.plot(mdates.num2date(_xn), _spl(_xn), linewidth=_lw, alpha=_al, color=_col, label=label, zorder=4)
        else:
            ax.plot(ds, ys, linewidth=_lw, alpha=_al, color=_col, label=label, zorder=4)
        _x_last = xs[-1]
    if hlines and _x_last is not None:
        for _hv, _hl in hlines:
            ax.axhline(_hv * 100, color='#777', linestyle='--', linewidth=1.4, zorder=3)
            ax.text(0.99, _hv * 100, _hl, transform=ax.get_yaxis_transform(),
                    rotation=90, ha='center', va='center', fontsize=8, color='#555',
                    bbox=dict(facecolor='white', edgecolor='none', pad=1.5), zorder=5)
    if bars:
        _bxs = mdates.date2num([d for d, _ in bars])
        _bys = [v * 100 for _, v in bars]
        _bcol = ['#c0392b' if v < 0 else '#2e7d32' for v in _bys]
        ax.bar(_bxs, _bys, width=150, color=_bcol, alpha=0.45, zorder=2)
    ax.axhline(0, color='#888', linewidth=0.8)
    ax.set_title(title, fontsize=12, weight='bold')
    ax.set_ylabel(ylabel)
    # major ticks at year-ENDS (Dec 31) labeled with that year, so an annual
    # point never visually merges with the next January tick
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=12, bymonthday=31))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y'))
    ax.xaxis.set_minor_locator(mdates.MonthLocator(bymonth=(1, 4, 7, 10)))
    ax.margins(x=0.04)
    if legend and len(_ordered) > 1:
        ax.legend(fontsize=8, loc='best')
    buf = io.BytesIO(); fig.savefig(buf, format='png'); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()

# ---------------- portfolio view ----------------
rows_port = []
for p in port_names:
    if port_total[p] <= 0: continue
    rows_port.append(f"<h2>{E(p)}</h2>\n<table><tr><th>Account</th><th>Market value</th><th>% of portfolio</th><th>Cost basis</th><th>Unrealized G/L</th></tr>")
    for m in acct_order:
        prs = [r for r in positions if r['port'] == p and r['mask'] == m]
        if not prs: continue
        pv = sum(r['val'] for r in prs); pcb = sum(r['cb'] for r in prs); pgl = sum(r['gl'] for r in prs)
        rows_port.append(f"<tr><td>{E(acct_label(m))}</td><td class=n>{money(pv)}</td><td class=n>{pct(pv/port_total[p])}</td><td class=n>{money(pcb)}</td><td class=n>{money(pgl)}</td></tr>")
    tcb = sum(r['cb'] for r in positions if r['port'] == p)
    tgl = sum(r['gl'] for r in positions if r['port'] == p)
    rows_port.append(f"<tr class=tot><td>Total</td><td class=n>{money(port_total[p])}</td><td class=n>100.0%</td><td class=n>{money(tcb)}</td><td class=n>{money(tgl)}</td></tr></table>")

# ---------------- historical yield ----------------
import datetime as _dt
year_start = _dt.date(asof_d.year, 1, 1)
def ticker_ret(t, period):
    ov = _ovr_rate(t)
    if period == "ytd":
        if ov is not None:
            return ov * (asof_d - year_start).days / 365.25
        pts = hist.get(t)
        if not pts: return None
        ds = [(_dt.date.fromisoformat(d), p) for d, p in pts]
        end_d, end_p = ds[-1]
        prev = [(d, p) for d, p in ds if d < year_start]
        if not prev: return None
        return end_p / prev[-1][1] - 1
    if ov is not None: return ov
    pts = hist.get(t)
    if not pts: return None
    ds = [(_dt.date.fromisoformat(d), p) for d, p in pts]
    end_d, end_p = ds[-1]
    tgt = end_d - _dt.timedelta(days=period)
    past = [(d, p) for d, p in ds if d <= tgt]
    if not past: return None
    sd, sp = past[-1]
    yrs = (end_d - sd).days / 365.25
    return (end_p / sp) ** (1 / yrs) - 1 if yrs > 0 else None

periods = [("YTD", "ytd"), ("1-year", 365), ("3-year", 1096), ("5-year", 1826), ("10-year", 3652)]
yield_rows = []
for p in port_names:
    if port_total[p] <= 0: continue
    cells = []
    for pname, per in periods:
        _yn = _yd = 0.0
        for r in positions:
            if r['port'] != p: continue
            ar = ticker_ret(r['ticker'], per)
            if ar is None: continue
            _yn += ar * r['val']; _yd += r['val']
        cells.append(f"{_yn/_yd*100:.1f}%" if _yd > 0 else "n/a")
    yield_rows.append((p, cells))
total_cells = []
for pname, per in periods:
    _tn, _td = 0.0, 0.0
    for r in positions:
        ar = ticker_ret(r['ticker'], per)
        if ar is None: continue
        _tn += ar * r['val']; _td += r['val']
    total_cells.append(f"{_tn/_td*100:.1f}%" if _td > 0 else "n/a")
yield_rows.append(("Total", total_cells, True))
excluded = {}
for r in positions:
    if _ovr_rate(r['ticker']) is None and r['ticker'] not in hist:
        excluded[r['ticker']] = excluded.get(r['ticker'], 0.0) + r['val']
excluded_txt = ", ".join(f"{t} ({money(v)})" for t, v in sorted(excluded.items())) or "none"
over_txt = ", ".join(f"{t} at a constant {yield_overrides[t]*100:.0f}%/yr" for t in sorted(yield_overrides)) or "none"

def _yr(row):
    tot = len(row) == 3
    tds = "".join(f"<td class=n>{c}</td>" for c in row[1])
    return f"<tr{' class=tot' if tot else ''}><td>{E(row[0])}</td>{tds}</tr>"
yield_html = (
    "<h2>Holdings Historical Yield</h2>\n"
    f"<p class=src>YTD is the simple return since Dec 31; 1/3/5/10-year figures are annualized average total returns for the trailing periods ending {E(str(asof))}, weighted by each holding's market value.</p>\n"
    "<table><tr><th>Portfolio</th><th>YTD</th><th>1-year</th><th>3-year</th><th>5-year</th><th>10-year</th></tr>\n"
    + "".join(_yr(row) for row in yield_rows) + "</table>\n"
    f"<p class=src>Source: dividend-and-split-adjusted closing prices. Excludes holdings with no market price history: {E(excluded_txt)}. Past performance is backward-looking and does not predict future results."
    + (f" Yield override applied: {E(over_txt)}." if yield_overrides else "") + "</p>\n"
)

# ---- historical yield time series: trailing-1yr total return at each quarter-end ----
import bisect as _bisect
_hist_sorted = {t: sorted((_dt.date.fromisoformat(d), p) for d, p in pts) for t, pts in hist.items()}
_hist_dstr = {t: [d for d, _ in v] for t, v in _hist_sorted.items()}
def _ret_at(ticker, end_d, days=365):
    ov = _ovr_rate(ticker)
    if ov is not None:
        return ov
    v = _hist_sorted.get(ticker)
    if not v: return None
    ds = _hist_dstr[ticker]
    i = _bisect.bisect_right(ds, end_d) - 1
    if i < 0: return None
    j = _bisect.bisect_right(ds, end_d - _dt.timedelta(days=days)) - 1
    if j < 0 or v[j][1] == 0: return None
    return v[i][1] / v[j][1] - 1
def _qends(a, b):
    out = []
    y = a.year
    while True:
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qd = _dt.date(y, m, d)
            if a <= qd <= b:
                out.append(qd)
        y += 1
        if _dt.date(y, 1, 1) > b:
            break
    return out
_yq = _qends(_dt.date(asof_d.year - 10, asof_d.month, 1), asof_d)  # 10-year x scale
def _yield_series(prs):
    pv = sum(r['val'] for r in prs)
    s = []
    for qd in _yq:
        n = d_ = 0.0
        for r in prs:
            ar = _ret_at(r['ticker'], qd)
            if ar is None: continue
            n += ar * r['val']; d_ += r['val']
        if d_ >= 0.5 * pv and d_ > 0:
            s.append((qd, n / d_))
    return s
yield_series = []
for p in port_names:
    if port_total[p] <= 0: continue
    _s = _yield_series([r for r in positions if r['port'] == p])
    if _s: yield_series.append((p, _s))
_ts = _yield_series(positions)
if _ts: yield_series.append(("Total", _ts))
# Total's trailing 10-year annualized average -> horizontal reference line
_avg10_n = _avg10_d = 0.0
for r in positions:
    ar = ticker_ret(r['ticker'], 3652)
    if ar is None: continue
    _avg10_n += ar * r['val']; _avg10_d += r['val']
_avg10 = _avg10_n / _avg10_d if _avg10_d > 0 else None
_hl = [(_avg10, f"10-yr avg {_avg10*100:.1f}%")] if _avg10 is not None else None
# (yield chart is rendered after the TWR section so it can overlay the TWR curve)


# ---------------- gross TWR (yearly summary CSV) ----------------
twr_html = ""
twr_chart_b64 = ""
yield_chart_b64 = ""
_twr_pts = []
_twr_bars = []
SUM_CSV = None if (not SUM_CSV_ARG or SUM_CSV_ARG.lower() == 'none') else SUM_CSV_ARG
TXN_ARG = sys.argv[3] if len(sys.argv) > 3 else ''
TXN_CSV = None if TXN_ARG.lower() == 'none' else (TXN_ARG or os.path.join(os.path.dirname(CONFIG), 'Accounts_History_Merged.csv'))
if SUM_CSV and os.path.exists(SUM_CSV):
    qall = list(csv.DictReader(open(SUM_CSV, encoding='utf-8-sig', newline='')))
    srows = [r for r in qall if r.get('Period', 'Annual') == 'Annual']
    def fnum2(x): return float(str(x).replace(',', ''))
    years = sorted(set(int(r['Year']) for r in srows), reverse=True)
    # table shows the current year's quarterly columns plus the previous 5 annual years only
    _qy = max((int(r['Year']) for r in qall
               if str(r.get('Period', '')).strip().upper() in ('Q1', 'Q2', 'Q3', 'Q4')), default=None)
    if _qy is not None:
        years = [y for y in years if _qy - 5 <= y <= _qy - 1]
    else:
        years = years[:5]
    fees_y = defaultdict(float)
    fees_yq = defaultdict(float)
    if TXN_CSV and os.path.exists(TXN_CSV):
        for r in csv.DictReader(open(TXN_CSV, encoding='utf-8-sig', newline='')):
            a = (r['Action'] or '').upper()
            if 'ADVISOR FEE DEDUCTED' in a or 'FEE CHARGED ALT INV' in a:
                _d = pd(r['Run Date'])
                fees_y[_d.year] += abs(num(r['Amount ($)']))
                fees_yq[(_d.year, (_d.month - 1) // 3 + 1)] += abs(num(r['Amount ($)']))
    cols = []
    twr_vals = []
    _twr_year_vals = []  # year-aligned (None kept) for the TWR chart
    for y in years:
        yr = [r for r in srows if int(r['Year']) == y]
        st = sum(fnum2(r['Beginning Value']) for r in yr)
        fl = sum(fnum2(r['Net Changes']) for r in yr)
        iv = sum(fnum2(r['Investment Value Change']) for r in yr)
        en = sum(fnum2(r['Ending Value']) for r in yr)
        fee = fees_y.get(y, 0.0)
        t = (iv + fee) / (st + 0.5 * fl) if (st + 0.5 * fl) else None
        cols.append((money(st), money(fl), money(iv), money(en), f"{t*100:.1f}%" if t is not None else "n/a"))
        _twr_year_vals.append((y, t))
        if t is not None: twr_vals.append(t)
    # ---- current-year cumulative quarters: YTD from Jan 1 through each quarter end ----
    qheads = []
    qcols = []
    _qtwr_pts = []  # (quarter-end date, TWR float) for the TWR chart
    _qrows = [r for r in qall if str(r.get('Period', '')).strip().upper() in ('Q1', 'Q2', 'Q3', 'Q4')]
    if _qrows:
        _cy = max(int(r['Year']) for r in _qrows)
        # Quarterly columns are cumulative YTD, so every account in the annual table needs
        # Q1 data — either a real Q1 row or a derivable one (Q2 row + prior year-end).
        # Otherwise a "combined" quarterly column would silently exclude accounts, so skip them.
        _ann_accts = set(r['Account'] for r in srows if int(r['Year']) in years)
        _qmap0 = {}
        for r in _qrows:
            if int(r['Year']) == _cy:
                _qmap0[(r['Account'], str(r['Period']).strip().upper())] = r
        _ann0 = {(r['Account'], int(r['Year'])): r for r in srows}
        def _has_q1(_a):
            if (_a, 'Q1') in _qmap0:
                return True
            return (_a, 'Q2') in _qmap0 and (_a, _cy - 1) in _ann0
        if not all(_has_q1(_a) for _a in _ann_accts):
            _qrows = []
    if _qrows:
        _cy = max(int(r['Year']) for r in _qrows)
        _qmap = {}
        for r in _qrows:
            if int(r['Year']) == _cy:
                _qmap[(r['Account'], str(r['Period']).strip().upper())] = r
        _ann = {(r['Account'], int(r['Year'])): r for r in srows}
        _accts = sorted(set(a for a, _ in _qmap))
        _qd = {}
        for _a in _accts:
            _d = {}
            for _qi in (1, 2, 3, 4):
                _r = _qmap.get((_a, f'Q{_qi}'))
                if _r is not None:
                    _d[_qi] = (fnum2(_r['Beginning Value']), fnum2(_r['Net Changes']), fnum2(_r['Ending Value']))
            if _d:
                if 1 not in _d and 2 in _d and (_a, _cy - 1) in _ann:
                    # missing Q1 row: begin at prior year-end, end at Q2 begin, no flow split
                    _d[1] = (fnum2(_ann[(_a, _cy - 1)]['Ending Value']), 0.0, _d[2][0])
                _qd[_a] = _d
        for _qi in (4, 3, 2, 1):  # newest first; future quarters stay blank
            qheads.append(f"{_cy}-Q{_qi}")
            _qend = _dt.date(_cy, {1: 3, 2: 6, 3: 9, 4: 12}[_qi], {1: 31, 2: 30, 3: 30, 4: 31}[_qi])
            if _qd and all(_qi in _d for _d in _qd.values()):
                _st = sum(_d[1][0] for _d in _qd.values())
                _fl = sum(_d[_q][1] for _d in _qd.values() for _q in range(1, _qi + 1))
                _en = sum(_d[_qi][2] for _d in _qd.values())
                _iv = _en - _st - _fl
                _fee = sum(fees_yq.get((_cy, _q), 0.0) for _q in range(1, _qi + 1))
                _t = (_iv + _fee) / (_st + 0.5 * _fl) if (_st + 0.5 * _fl) else None
                qcols.append((money(_st), money(_fl), money(_iv), money(_en),
                              f"{_t*100:.1f}%" if _t is not None else "n/a"))
                if _t is not None:
                    _qtwr_pts.append((_qend, _t))
            else:
                qcols.append(("", "", "", "", ""))
    cols = qcols + cols
    _tc = lambda i: "".join(f"<td class=n>{c[i]}</td>" for c in cols)
    _yh = "".join(f"<th>{h}</th>" for h in qheads) + "".join(f"<th>{y}</th>" for y in years)
    def _avg(vals):
        _g = 1.0
        for _v in vals:
            _g *= (1 + _v)
        return _g ** (1 / len(vals)) - 1
    # twr_vals is newest-first: 1/3/5yr geometric means
    _parts = []
    for _n, _lbl in ((1, "1yr"), (3, "3yr"), (5, "5yr")):
        _w = twr_vals[:_n]
        _parts.append(f"{_lbl} avg: {_avg(_w)*100:.1f}%" if _w else f"{_lbl} avg: n/a")
    geo_txt = ", ".join(_parts)
    # ---- Gross TWR chart: annual bars + smooth line through annuals and quarterly YTD ----
    _twr_pts = [(_dt.date(y, 12, 31), t) for y, t in _twr_year_vals if t is not None] + _qtwr_pts
    _twr_bars = [(_dt.date(y, 12, 31), t) for y, t in _twr_year_vals if t is not None]
    twr_html = (
        "<h2>Gross TWR</h2>\n"
        "<table><tr><th></th>" + _yh + "</tr>\n"
        f"<tr><td>Start Value</td>{_tc(0)}</tr>\n"
        f"<tr><td>Net Flows</td>{_tc(1)}</tr>\n"
        f"<tr><td>Investment Change</td>{_tc(2)}</tr>\n"
        f"<tr><td>End Value</td>{_tc(3)}</tr>\n"
        f"<tr class=tot><td>Gross TWR</td>{_tc(4)}</tr></table>\n"
        "<p class=src>Annual time-weighted return"
        + (", gross of fees" if TXN_CSV else " (fee data not available for these accounts)")
        + ": (Investment Value Change"
        + (" + fees" if TXN_CSV else "")
        + ") &divide; (Beginning Value + &frac12; &times; Net Flows); "
        "flows assumed mid-year. Quarterly columns are cumulative year-to-date from Jan 1 through "
        "each quarter end"
        + (", gross of fees deducted through that quarter" if TXN_CSV else "")
        + "; future quarters are blank. "
        "Sources: account statements CSV"
        + ("; fees from transaction history" if TXN_CSV else "")
        + ".</p>\n"
        f"<p><b>Gross TWR annual averages</b> &mdash; {geo_txt}.</p>\n"
    )

# ---- shared y scale + the two return charts ----
_allv = [t * 100 for _, t in _twr_pts] + [ar * 100 for _, pts in yield_series for _, ar in pts]
_shared_ylim = None
if _allv:
    _yr = max(_allv) - min(_allv) or 1
    _shared_ylim = (min(_allv) - _yr * 0.08, max(_allv) + _yr * 0.08)
twr_chart_b64 = _line_chart([("Gross TWR", _twr_pts)], "Gross TWR", "Return %",
                            legend=False, bars=_twr_bars, ylim=_shared_ylim,
                            emphasis={"Gross TWR": "#e65100"},
                            figsize=(2.8, 4.2)) if _twr_pts else ""
# ---- historical yield chart: yield lines + Gross TWR curve overlaid (line only) ----
_ywt = yield_series + ([("Gross TWR", _twr_pts)] if _twr_pts else [])
yield_chart_b64 = _line_chart(_ywt, "Historical Yield — trailing 1-yr", "Return %",
                              emphasis={"Gross TWR": "#e65100", "Total": "#1a1a1a"},
                              hlines=_hl, ylim=_shared_ylim) if _ywt else ""

# ---------------- per-account view ----------------
rows_acct = []
for m in acct_order:
    if acct_total[m] <= 0:
        rows_acct.append(f"<h2>{E(acct_label(m))}</h2><p>No holdings returned for this account.</p>")
        continue
    rows_acct.append(f"<h2>{E(acct_label(m))}</h2>\n<p class=tline>{money(acct_total[m])} market value · {money(acct_cb[m])} cost basis · {money(acct_gl[m])} unrealized gain/loss</p>")
    for p in port_names:
        prs = [r for r in positions if r['mask'] == m and r['port'] == p]
        if not prs: continue
        rows_acct.append(f"<h3>{E(p)}</h3>\n<table><tr><th>Symbol</th><th>Description</th><th>Shares</th><th>Price</th><th>Market value</th><th>% of acct</th><th>Cost basis</th><th>Unrealized G/L</th></tr>")
        for r in sorted(prs, key=lambda x: -x['val']):
            rows_acct.append(f"<tr><td>{E(r['ticker'])}</td><td>{E(r['desc'])}</td><td class=n>{r['qty']:,.3f}</td><td class=n>{r['price']:,.2f}</td><td class=n>{money(r['val'])}</td><td class=n>{pct(r['val']/acct_total[m])}</td><td class=n>{money(r['cb'])}</td><td class=n>{money(r['gl'])}</td></tr>")
        rows_acct.append("</table>")

# ---------------- assemble ----------------
unmapped_cfg = []
allids = set()
for r in positions:
    allids |= {r['ticker'], r['desc']}
for p in portfolios:
    for sym in p['symbols']:
        if not any(sym and sym in x for x in allids):
            unmapped_cfg.append(f"{sym} (under {p['name']})")
flags = []
if unmapped:
    prop = "; ".join(f"{t} — {d or '?'} [{s or '?'}] ({money(v)})"
                     for t, (d, s, v) in sorted(unmapped.items()))
    flags.append("PROPOSED CLASSIFICATIONS (needs your approval to add to the config): " + prop)
if unmapped_cfg: flags.append("Config symbols matching no holding: " + ", ".join(unmapped_cfg))
flag_html = ("<ul class=flags>" + "".join(f"<li>{E(f)}</li>" for f in flags) + "</ul>") if flags else ""

# ---- no statements CSV: skip the TWR chart/section/overlay entirely, keep the rest ----
_twr_chart_div = (f'<div><img src="data:image/png;base64,{twr_chart_b64}" alt="Gross TWR chart"></div>'
                  if twr_chart_b64 else "")
# TWR chart at half the yield chart's width: its ~6yr x-range then matches the
# yield chart's 10yr x-range in time-per-pixel, so line slopes read the same.
_twr_chart_div_narrow = _twr_chart_div.replace('<div>', '<div class=narrow>', 1)
# second donut: US vs foreign within the split-covered global holdings
_us_foreign_div = (f'<div class=charts><div><img src="data:image/png;base64,{us_foreign_b64}" alt="US vs foreign donut chart"></div></div>'
                  if us_foreign_b64 else "")
if twr_chart_b64:
    _cap = ("Gross TWR: bars are the annual returns at each year-end; the smooth line runs through the annuals "
            "and continues through the cumulative year-to-date quarterly points. Historical yield: trailing 1-year "
            "total return at each quarter-end over a 10-year scale, with the Total line emphasized; the thick orange "
            "line overlays the Gross TWR curve (line only, no bars) for comparison. Both return charts share the "
            "same y-axis scale. The dashed line is the Total's trailing 10-year annualized average. Curves are "
            "smoothed (flat clamped start); both x-axes are true calendar time.")
else:
    _cap = ("Historical yield: trailing 1-year total return at each quarter-end over a 10-year scale, with the "
            "Total line emphasized. The dashed line is the Total's trailing 10-year annualized average. Curves are "
            "smoothed (flat clamped start); the x-axis is true calendar time. "
            "(No statements CSV was supplied, so the Gross TWR section is omitted.)")

html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{E(REPORT_TITLE)} Summary — As of {E(str(asof))}</title>
<style>body{{font-family:-apple-system,'Segoe UI',system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #ccc;padding:7px 10px;text-align:left;font-size:14px}}
th{{background:#f2f2f2}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}tr.tot td{{font-weight:bold;background:#f8f8f8}}
h1{{font-size:24px}}h2{{font-size:19px;margin-top:30px}}h3{{font-size:16px;margin-top:20px}}.tline{{font-weight:bold}}
.flags{{background:#fff8e1;border:1px solid #e6c200;padding:10px 16px;border-radius:6px}}.src{{color:#555;font-size:14px}}
.charts{{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap;margin:8px 0 20px}}
.charts div{{flex:1;min-width:340px}}.charts div.narrow{{flex:0.5;min-width:200px}}.charts img{{max-width:100%;width:100%;height:auto;display:block}}</style></head>
<body><h1>{E(REPORT_TITLE)} Summary — As of {E(str(asof))}</h1>
<p class=src>Live Plaid data · {len(positions)} positions across {len([m for m in acct_order if acct_total[m]>0])} accounts · {E(', '.join(brokers))} · prices as of {E(str(asof))}</p>
<h2>Holdings by Portfolio</h2>
<div class=charts><div><img src="data:image/png;base64,{chart_b64}" alt="Portfolio allocation donut chart"></div></div>
{_us_foreign_div}
<div class=charts>{_twr_chart_div_narrow}
<div><img src="data:image/png;base64,{yield_chart_b64}" alt="Historical yield chart"></div></div>
<p class=src>{_cap}</p>
{''.join(rows_port)}
{yield_html}
{twr_html}
<h2>Holdings by Account</h2>
{''.join(rows_acct)}
<h2>Combined total</h2>
<p class=tline>{money(grand)} market value · {money(sum(acct_cb.values()))} cost basis · {money(sum(acct_gl.values()))} unrealized gain/loss</p>
{flag_html}
</body></html>"""

out = os.path.join(OUTDIR, f"{OUT_PREFIX}holdings-summary-{STAMP}.html")
open(out, 'w').write(html_doc)
print(f"wrote {out} ({os.path.getsize(out)//1024} KB) · {len(positions)} positions · {money(grand)}")
