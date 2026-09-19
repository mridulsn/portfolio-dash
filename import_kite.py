"""
Import a Kite/Console "holdings-<client>.xlsx" statement into holdings.local.json.

  python import_kite.py "D:\\Downloads\\holdings-CLIENTID.xlsx"          # write local file only
  python import_kite.py "D:\\Downloads\\holdings-CLIENTID.xlsx" --push   # also update the GitHub secret

Sheet 1 (Equity) -> stocks, sheet 2 (Mutual Funds) -> mf. The statement date becomes "asof":
SIP instalments dated AFTER it are added automatically by build.py, so a fresh statement simply
resets the baseline. The "sips" list already in holdings.local.json is kept untouched.
"""
import sys, os, re, json, subprocess
sys.stdout.reconfigure(encoding="utf-8")
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.join(HERE, "holdings.local.json")
REPO = "mridulsn/portfolio-dash"

CAT_MAP = {
    "Equity - Large & Mid Cap": "Large & Mid Cap", "Equity - Large Cap": "Large Cap",
    "Equity - Mid Cap": "Mid Cap", "Equity - Small Cap": "Small Cap", "Equity - Flexi Cap": "Flexi Cap",
    "Equity - ELSS": "ELSS", "Hybrid - Aggressive": "Hybrid Aggressive",
    "Others - Index Funds/ETFs": "Index", "Others - Fund of Funds": "Fund of Funds",
}
SMALL = {"and", "of", "&"}

def nice_name(raw):
    s = re.sub(r"\s*-\s*DIRECT PLAN\s*$", "", raw.strip(), flags=re.I)
    s = re.sub(r"\s+FUND$", "", s).replace(" - ", " ")
    words = []
    for w in s.split():
        lw = w.lower()
        if lw in SMALL: words.append(lw)
        elif w in ("HDFC", "ICICI", "ELSS", "IDCW"): words.append(w)
        else: words.append(w.capitalize())
    return " ".join(words)

def read_sheet(ws):
    rows = [r for r in ws.iter_rows(values_only=True)]
    asof = None
    for r in rows[:20]:
        for c in r:
            m = re.search(r"as on (\d{4}-\d{2}-\d{2})", str(c or ""))
            if m: asof = m.group(1)
    hdr_i = next(i for i, r in enumerate(rows) if r and "Symbol" in [str(c).strip() if c else "" for c in r])
    hdr = [str(c).strip() if c else "" for c in rows[hdr_i]]
    out = []
    for r in rows[hdr_i + 1:]:
        rec = {hdr[i]: r[i] for i in range(len(hdr)) if hdr[i]}
        if rec.get("Symbol"): out.append(rec)
    return asof, out

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args: raise SystemExit(__doc__)
    wb = openpyxl.load_workbook(args[0], data_only=True)
    asof_s, eq = read_sheet(wb.worksheets[0])
    asof_m, mf = read_sheet(wb.worksheets[1])
    if not eq and not mf: raise SystemExit("No holdings rows found - is this a Kite holdings statement?")
    asof = asof_m or asof_s
    if not asof: raise SystemExit("Could not find the 'as on YYYY-MM-DD' statement date - refusing to guess")

    old = {}
    if os.path.exists(LOCAL):
        old = json.load(open(LOCAL, encoding="utf-8"))

    stocks = []
    for r in eq:
        qty = float(r.get("Quantity Available") or 0)
        if qty <= 0: continue
        sector = " ".join(w if (w.isupper() and len(w) <= 4 and w not in ("AND",)) else w.title() for w in str(r.get("Sector") or "").split())
        stocks.append([r["Symbol"], sector, qty, float(r["Average Price"]),
                       float(r.get("Quantity Long Term") or 0), r.get("ISIN")])
    funds = []
    for r in mf:
        units = float(r.get("Quantity Available") or 0)
        if units <= 0: continue
        funds.append([nice_name(r["Symbol"]), r["ISIN"], CAT_MAP.get(r.get("Instrument Type"), r.get("Instrument Type") or "Other"),
                      units, float(r["Average Price"])])

    new = {"asof": asof, "stocks": stocks, "mf": funds, "sips": old.get("sips", [])}
    # Dated purchase lots come from import_tradebook.py. A new statement can change quantities
    # (new buys, sells), so the old lots are kept but flagged stale until the tradebook is re-run.
    if old.get("lots"):
        new["lots"] = old["lots"]
        new["lots_info"] = dict(old.get("lots_info") or {}, stale_since=asof)
        print("NOTE: kept purchase dates from the last tradebook import - re-run import_tradebook.py"
              " so new buys/sells get their dates")
    held = {f[1] for f in funds}
    for s in new["sips"]:
        if s[0] not in held:
            print(f"WARNING: SIP fund {s[0]} ({s[3] if len(s) > 3 else ''}) is not in this statement")
    txt = json.dumps(new, ensure_ascii=True, separators=(",", ":"))
    open(LOCAL, "w", encoding="utf-8").write(txt)
    print(f"statement as on {asof}: {len(stocks)} stocks, {len(funds)} funds, {len(new['sips'])} SIPs kept")
    print("wrote", LOCAL)

    if "--push" in sys.argv:
        gh = r"C:\Program Files\GitHub CLI\gh.exe"
        subprocess.run([gh, "secret", "set", "HOLDINGS_JSON", "-R", REPO], input=txt.encode(), check=True)
        subprocess.run([gh, "workflow", "run", "refresh.yml", "-R", REPO], check=True)
        print("GitHub secret HOLDINGS_JSON updated and a refresh run started")

if __name__ == "__main__":
    main()
