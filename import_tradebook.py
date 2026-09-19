"""
Import Kite Console tradebook exports so the dashboard knows WHEN each holding was bought.

  python import_tradebook.py "D:\\Downloads\\tradebook-XXXX-EQ.xlsx" ["...-MF.xlsx" ...]          # local only
  python import_tradebook.py "D:\\Downloads\\tradebook-*.xlsx" --push                              # + GitHub secret

Why: the Kite holdings statement only says HOW MANY shares are long-term, never the buy dates.
Tax on a sale depends on each purchase lot's date (more than 12 months = long-term for listed
shares and equity funds), so this builds dated lots:

  * trades are matched first-in-first-out per stock (symbol) and per fund (ISIN) - the order
    Indian tax law uses when you sell part of a holding;
  * only trades on or before the holdings statement date ("asof") are used - SIP instalments
    after that are added by build.py from real NAVs, so nothing is counted twice;
  * the result is checked against the statement quantities. If the tradebook does not cover a
    whole holding (range started too late, a bonus/split, shares moved in from another broker)
    the uncovered part is stored as an UNDATED lot and shown as "date unknown" - never guessed.

Writes "lots" into holdings.local.json; the Kite importer keeps them on its next run.
Both CSV and XLSX exports work, and several files (e.g. one per year) can be passed at once.
"""
import sys, os, re, json, glob, csv, subprocess, datetime as dt
sys.stdout.reconfigure(encoding="utf-8")
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, "holdings.local.json")
REPO = "mridulsn/portfolio-dash"
TOL = 1e-3


def _norm(h):
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()[:10]
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def read_rows(path):
    """All tradebook rows of one file as dicts with normalised keys."""
    if path.lower().endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.reader(f))
        sheets = [raw]
    else:
        # NOT read_only: Zerodha's xlsx has no <dimension> tag, and read-only mode then sees a
        # single empty cell (found 2026-09-19 - it reported "0 trades")
        wb = openpyxl.load_workbook(path, data_only=True)
        sheets = [[list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    out = []
    for raw in sheets:
        hi = next((i for i, r in enumerate(raw) if r and "trade_date" in [_norm(c) for c in r]), None)
        if hi is None:
            continue
        head = [_norm(c) for c in raw[hi]]
        for r in raw[hi + 1:]:
            rec = {head[i]: r[i] for i in range(min(len(head), len(r))) if head[i]}
            if rec.get("trade_date") and rec.get("trade_type"):
                out.append(rec)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = sorted({f for a in args for f in (glob.glob(a) or [a])})
    if not files:
        raise SystemExit(__doc__)
    if not os.path.exists(LOCAL):
        raise SystemExit("holdings.local.json not found - import the Kite holdings statement first")
    H = json.load(open(LOCAL, encoding="utf-8"))
    asof = _date(H.get("asof"))
    if not asof:
        raise SystemExit("holdings.local.json has no 'asof' date - re-import the holdings statement first")

    trades, seen = [], set()
    for f in files:
        rows = read_rows(f)
        print(f"{os.path.basename(f)}: {len(rows)} trades")
        for r in rows:
            tid = (str(r.get("trade_id") or ""), str(r.get("order_id") or ""), str(r.get("symbol")),
                   str(r.get("trade_date")), str(r.get("quantity")), str(r.get("price")))
            if tid in seen:            # overlapping downloads - count each trade once
                continue
            seen.add(tid)
            d = _date(r["trade_date"])
            if not d:
                continue
            seg = str(r.get("segment") or "").upper()
            trades.append(dict(date=d, sym=str(r.get("symbol") or "").strip().upper(),
                               isin=str(r.get("isin") or "").strip().upper(),
                               mf=seg == "MF" or str(r.get("isin") or "").upper().startswith("INF"),
                               side=str(r["trade_type"]).strip().lower(),
                               qty=float(r.get("quantity") or 0), price=float(r.get("price") or 0),
                               t=str(r.get("order_execution_time") or "")))
    if not trades:
        raise SystemExit("No trades found - is this a Console tradebook export?")
    first = min(t["date"] for t in trades)
    trades = [t for t in trades if t["date"] <= asof]
    trades.sort(key=lambda t: (t["date"], t["t"], 0 if t["side"] == "buy" else 1))

    # FIFO per key
    book = {}
    for t in trades:
        key = ("mf", t["isin"]) if t["mf"] else ("eq", t["sym"])
        lots = book.setdefault(key, [])
        if t["side"] == "buy":
            lots.append([t["date"].isoformat(), t["qty"], t["price"]])
        else:
            q = t["qty"]
            while q > TOL and lots:
                take = min(q, lots[0][1])
                lots[0][1] -= take
                q -= take
                if lots[0][1] <= TOL:
                    lots.pop(0)
            # a sell larger than the recorded buys means the buys predate the files - harmless here

    out = {"eq": {}, "mf": {}}
    report = []
    for sym, _sec, qty, _avg, *rest in H["stocks"]:
        lots = [[d, round(q, 4), p] for d, q, p in book.get(("eq", sym.upper()), []) if q > TOL]
        have = sum(l[1] for l in lots)
        if have > qty + TOL:           # more than held: keep the newest (FIFO sold the oldest)
            keep, lots2 = qty, []
            for l in reversed(lots):
                if keep <= TOL:
                    break
                q = min(keep, l[1]); lots2.insert(0, [l[0], round(q, 4), l[2]]); keep -= q
            lots, have = lots2, qty
        if qty - have > TOL:
            lots.insert(0, [None, round(qty - have, 4), None])     # undated - older than the files
            report.append(f"  {sym}: {qty - have:g} of {qty:g} shares have NO date in these files")
        out["eq"][sym] = lots
    for name, isin, _cat, units, _avg in H["mf"]:
        lots = [[d, round(q, 4), p] for d, q, p in book.get(("mf", isin.upper()), []) if q > TOL]
        have = sum(l[1] for l in lots)
        if have > units + TOL:
            keep, lots2 = units, []
            for l in reversed(lots):
                if keep <= TOL:
                    break
                q = min(keep, l[1]); lots2.insert(0, [l[0], round(q, 4), l[2]]); keep -= q
            lots, have = lots2, units
        if units - have > 0.01:
            lots.insert(0, [None, round(units - have, 4), None])
            report.append(f"  {name[:40]}: {units - have:.3f} of {units:.3f} units have NO date in these files")
        out["mf"][isin] = lots

    H["lots"] = out
    H["lots_info"] = dict(files=[os.path.basename(f) for f in files], first_trade=first.isoformat(),
                          matched_to=asof.isoformat(),
                          built=dt.datetime.now().isoformat(timespec="seconds"))
    txt = json.dumps(H, ensure_ascii=True, separators=(",", ":"))
    open(LOCAL, "w", encoding="utf-8").write(txt)
    dated_eq = sum(1 for v in out["eq"].values() if v and all(l[0] for l in v))
    dated_mf = sum(1 for v in out["mf"].values() if v and all(l[0] for l in v))
    print(f"trades from {first} to {asof}: {len(trades)} used")
    print(f"fully dated: {dated_eq}/{len(out['eq'])} stocks, {dated_mf}/{len(out['mf'])} funds")
    if report:
        print("not fully covered (shown as 'date unknown' on the dashboard):")
        print("\n".join(report))
    print("wrote", LOCAL)
    if "--push" in sys.argv:
        gh = r"C:\Program Files\GitHub CLI\gh.exe"
        subprocess.run([gh, "secret", "set", "HOLDINGS_JSON", "-R", REPO], input=txt.encode(), check=True)
        subprocess.run([gh, "workflow", "run", "refresh.yml", "-R", REPO], check=True)
        print("GitHub secret HOLDINGS_JSON updated and a refresh run started")


if __name__ == "__main__":
    main()
