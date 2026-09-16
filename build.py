"""
Cloud build: Nifty 500 stock screen + MF screen + encrypted personal-holdings panel.
Designed to run in GitHub Actions. All secrets come from environment variables:
  DASH_PASSWORD  - password that decrypts the dashboard (required)
  HOLDINGS_JSON  - {"stocks":[[sym,sector,qty,avg],...],"mf":[[name,isin,cat,units,avg],...]}
                   (optional; falls back to the embedded defaults for local runs)
Output: ./public/index.html  (single encrypted file — safe to publish)
"""
import warnings; warnings.filterwarnings("ignore")
import sys; sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
import json, os, io, re, time, base64, hashlib, urllib.request, datetime as dt
import numpy as np, pandas as pd, yfinance as yf
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(HERE, "public"); os.makedirs(OUTDIR, exist_ok=True)
OUT = os.path.join(OUTDIR, "index.html")
TPL_PATH = os.path.join(HERE, "template.html")
N500_CSV = os.path.join(HERE, "nifty500.csv")
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': '*/*'}
def get(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=timeout).read()

# ---- holdings ----
# Real holdings NEVER live in this file (the repo is public). They come ONLY from:
#   1. the HOLDINGS_JSON secret (cloud / GitHub Actions), or
#   2. holdings.local.json in this folder (gitignored) for local test runs.
# Format: {"stocks":[[sym,sector,qty,avg],...], "mf":[[name,isin,cat,units,avg],...]}
_h = os.environ.get("HOLDINGS_JSON")
if not _h:
    _local = os.path.join(HERE, "holdings.local.json")
    if os.path.exists(_local): _h = open(_local, encoding="utf-8").read()
if not _h:
    raise SystemExit("No holdings: set the HOLDINGS_JSON env var or create holdings.local.json")
_j = json.loads(_h)
# stocks: [sym, sector, qty, avg, (long-term qty), (isin)]  mf: [name, isin, cat, units, avg]
STOCK_HOLDINGS = [(x[0], x[1], float(x[2]), float(x[3]), float(x[4]) if len(x) > 4 and x[4] is not None else 0.0)
                  for x in _j["stocks"]]
MF_HOLDINGS    = [tuple(x[:5]) for x in _j["mf"]]
# sips: [isin, monthly amount, day of month, display name]. Instalments dated AFTER "asof"
# (the statement date) are added automatically from the real NAV of the instalment day.
SIPS = [dict(isin=x[0], amount=float(x[1]), day=int(x[2]), name=(x[3] if len(x) > 3 else x[0]))
        for x in _j.get("sips", [])]
HOLDINGS_ASOF = _j.get("asof")
HELD_SYMS = {x[0] for x in STOCK_HOLDINGS}

PASSWORD = os.environ.get("DASH_PASSWORD")
if not PASSWORD:
    p = os.path.join(HERE, "dash_secret.txt")
    if os.path.exists(p): PASSWORD = open(p,encoding="utf-8").read().strip()
if not PASSWORD: raise SystemExit("DASH_PASSWORD not set")

# --from-cache: skip every download and re-render template.html from last_payload.json (local only)
FROM_CACHE = "--from-cache" in sys.argv
CACHE = os.path.join(HERE, "last_payload.json")

def write_encrypted(payload):
    ITER=200_000; salt,iv=os.urandom(16),os.urandom(12)
    key=hashlib.pbkdf2_hmac("sha256",PASSWORD.encode(),salt,ITER,dklen=32)
    ct=AESGCM(key).encrypt(iv,json.dumps(payload).encode(),None)
    enc=dict(salt=base64.b64encode(salt).decode(),iv=base64.b64encode(iv).decode(),
             ct=base64.b64encode(ct).decode(),iter=ITER)
    with open(TPL_PATH,encoding="utf-8") as f: tpl=f.read()
    assert tpl.count("/*__DATA__*/")==1, "template must contain exactly one /*__DATA__*/ marker"
    html=tpl.replace("/*__DATA__*/","const ENC="+json.dumps(enc)+";")
    tmp=OUT+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: f.write(html)
    os.replace(tmp,OUT)
    print("WROTE",OUT)

if FROM_CACHE:
    write_encrypted(json.load(open(CACHE,encoding="utf-8")))
    raise SystemExit(0)

# ----------------------------------------------------------------------------- 1. universe (bundled)
n500 = pd.read_csv(N500_CSV)
sym2sector = dict(zip(n500["Symbol"], n500["Industry"]))
universe = sorted(set(n500["Symbol"]) | HELD_SYMS)
for s,sec,*_ in STOCK_HOLDINGS: sym2sector.setdefault(s, sec)
print(f"universe: {len(universe)} symbols")

# ----------------------------------------------------------------------------- 2. prices (with retry)
def download(tickers):
    for attempt in range(3):
        try:
            d = yf.download(tickers, period="15mo", interval="1d", auto_adjust=True,
                            progress=False, group_by="ticker", threads=True)
            if d is not None and len(d): return d
        except Exception as e:
            print(f"  download attempt {attempt+1} failed: {repr(e)[:80]}")
        time.sleep(10)
    raise SystemExit("price download failed after retries")
data = download([s+".NS" for s in universe])
last_bar = str(data.index[-1].date())

def adx(df, n=14):
    h,l,c = df["High"],df["Low"],df["Close"]
    pdm=h.diff(); mdm=-l.diff()
    pdm=pdm.where((pdm>mdm)&(pdm>0),0.0); mdm=mdm.where((mdm>pdm)&(mdm>0),0.0)
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/n,adjust=False).mean()
    pdi=100*pdm.ewm(alpha=1/n,adjust=False).mean()/atr
    mdi=100*mdm.ewm(alpha=1/n,adjust=False).mean()/atr
    dx=100*(pdi-mdi).abs()/(pdi+mdi).replace(0,np.nan)
    return float(dx.ewm(alpha=1/n,adjust=False).mean().iloc[-1])
def rr(c,n): return float(c.iloc[-1]/c.iloc[-n]-1) if len(c)>n else np.nan

rows=[]
for s in universe:
    try:
        df=data[s+".NS"].dropna()
        if len(df)<150: continue
        c=df["Close"]; px=float(c.iloc[-1]); ret=c.pct_change().dropna()
        ma20,ma50,ma200=c.rolling(20).mean().iloc[-1],c.rolling(50).mean().iloc[-1],c.rolling(200).mean().iloc[-1]
        vol=float(ret.std()*np.sqrt(252)); downside=float(ret[ret<0].std()*np.sqrt(252)) or np.nan
        sharpe=float(ret.mean()/ret.std()*np.sqrt(252)) if ret.std()>0 else np.nan
        sortino=float(ret.mean()*252/downside) if downside and downside==downside else np.nan
        cum=(1+ret).cumprod(); maxdd=float((cum/cum.cummax()-1).min())
        hi52=float(c.iloc[-252:].max())
        m=c.resample("ME").last().pct_change().dropna().iloc[-12:] if len(c)>260 else c.pct_change().dropna()
        consist=float((m>0).mean()) if len(m) else np.nan
        rows.append(dict(sym=s,sector=sym2sector.get(s,"—"),px=px,
            r1d=rr(c,2),r1w=rr(c,6),r1m=rr(c,21),r3m=rr(c,63),r6m=rr(c,126),r1y=rr(c,252),
            adx=adx(df),vol=vol,sharpe=sharpe,sortino=sortino,maxdd=maxdd,consist=consist,
            dist_hi=float(px/hi52-1),a20=bool(px>ma20),a50=bool(px>ma50),a200=bool(px>ma200),
            dist50=float(px/ma50-1)))
    except Exception: continue
d=pd.DataFrame(rows)
print(f"screened {len(d)} stocks, last bar {last_bar}")

def z(col):
    s=d[col]; return (s-s.mean())/s.std(ddof=0)
d["mom_z"]=(z("r1m")+z("r3m")+z("r6m"))/3; d["risk_z"]=z("sharpe"); d["trend_z"]=z("adx"); d["vol_z"]=z("vol")
d["score"]=(1.0*d["mom_z"]+0.6*d["risk_z"]+0.5*d["trend_z"]-0.25*d["vol_z"]
            +0.3*d["a200"].astype(int)+0.15*d["a50"].astype(int)).round(2)
d["stab"]=(-z("vol")-z("maxdd")+z("consist")+z("sharpe")*0.5+0.4*d["a200"].astype(int)).round(2)
d["pctile"]=(d["score"].rank(pct=True)*100).round()
def signal(x):
    up=x.a50 and x.a200 and x.adx>20
    if up and x.dist50>0.20: return "EXTENDED"
    if up: return "BUY-WATCH"
    if x.score<-1.2 or (not x.a200 and x.r3m<0): return "AVOID"
    return "NEUTRAL"
d["tag"]=d.apply(signal,axis=1)
d=d.sort_values("score",ascending=False).reset_index(drop=True)
def topn(df,by,n=10,need_up=True):
    x=df[df.a50&df.a200] if need_up else df
    return x.sort_values(by,ascending=False).head(n)
def pack(df):
    out=[]
    for _,x in df.iterrows():
        f=lambda v: round(v*100,1) if v==v else 0
        out.append(dict(sym=x.sym,sector=x.sector,px=round(x.px,1),r1d=f(x.r1d),r1w=f(x.r1w),
            r1m=f(x.r1m),r3m=f(x.r3m),r6m=f(x.r6m),r1y=f(x.r1y),adx=round(x.adx),vol=round(x.vol*100),
            sharpe=round(x.sharpe,2) if x.sharpe==x.sharpe else 0,maxdd=round(x.maxdd*100),
            dist50=round(x.dist50*100,1),score=x.score,stab=x.stab,pctile=int(x.pctile),
            tag=x.tag,held=x.sym in HELD_SYMS))
    return out
stocks_all=pack(d); top_day=pack(topn(d,"r1d")); top_week=pack(topn(d,"r1w")); top_month=pack(topn(d,"r1m"))
stable=pack(d[d.a200&(d.consist>=0.6)].sort_values("stab",ascending=False).head(12))

# ----------------------------------------------------------------------------- 3. mutual funds
amfi=get('https://www.amfiindia.com/spages/NAVAll.txt').decode('utf-8','ignore')
isin2code,code2name,name_index={},{},[]
for ln in amfi.splitlines():
    p=ln.split(';')
    if len(p)>=6 and p[0].strip().isdigit():
        code=p[0].strip(); nm=' '.join(x.strip() for x in p[3:len(p)-2] if x.strip() and x.strip()!='-')   # AMFI split plan/option into their own fields in 2026
        code2name[code]=nm; name_index.append((code,nm.lower()))
        for i in (p[1].strip(),p[2].strip()):
            if i and i!='-': isin2code[i]=code
NAV_CACHE={}
def hist(code):
    if code in NAV_CACHE: return NAV_CACHE[code]
    j=json.loads(get(f'https://api.mfapi.in/mf/{code}',timeout=30))
    s=pd.DataFrame(j['data']); s['date']=pd.to_datetime(s['date'],format='%d-%m-%Y')
    s['nav']=pd.to_numeric(s['nav'],errors='coerce')
    s=s.dropna().sort_values('date').set_index('date')['nav']
    NAV_CACHE[code]=s
    return s
def cagr(s,days):
    if len(s)<2: return np.nan
    w=s[s.index<=s.index[-1]-pd.Timedelta(days=days)]
    if w.empty: return np.nan
    yrs=days/365; r=s.iloc[-1]/w.iloc[-1]
    return float(r**(1/yrs)-1) if yrs>=1 else float(r-1)
def fund_metrics(code,name,cat):
    s=hist(code); ret=s.pct_change().dropna()
    sharpe=float(ret.mean()/ret.std()*np.sqrt(252)) if ret.std()>0 else np.nan
    cum=(1+ret).cumprod(); maxdd=float((cum/cum.cummax()-1).min())
    mm=s.resample('ME').last().pct_change().dropna().iloc[-36:]
    return dict(code=code,name=name,cat=cat,nav=round(float(s.iloc[-1]),4),
        nav_prev=float(s.iloc[-2]) if len(s)>1 else float(s.iloc[-1]),nav_date=str(s.index[-1].date()),
        nav_prev_date=str(s.index[-2].date()) if len(s)>1 else None,
        r1m=cagr(s,30),r3m=cagr(s,91),r6m=cagr(s,182),r1y=cagr(s,365),r3y=cagr(s,1095),r5y=cagr(s,1825),
        sharpe=sharpe,maxdd=maxdd,consist=float((mm>0).mean()) if len(mm) else np.nan)
def find_code(kw):
    kw=[k.lower() for k in kw]
    c=[(a,n) for a,n in name_index if all(k in n for k in kw) and 'direct' in n and 'growth' in n]
    if not c: c=[(a,n) for a,n in name_index if all(k in n for k in kw) and 'direct' in n]
    return min(c,key=lambda x:len(x[1]))[0] if c else None
CURATED={
 "Flexi Cap":[["parag","parikh","flexi"],["hdfc","flexi","cap"],["quant","flexi"],["jm","flexi"]],
 "Large Cap":[["icici","erstwhile","bluechip"],["nippon","large","cap"],["hdfc","top","100"]],
 "Large & Mid Cap":[["bajaj","finserv","large","mid"],["motilal","large","mid"],["kotak","equity","opportunities"],["sbi","large","midcap"],["navi","large"]],
 "Mid Cap":[["motilal","midcap"],["hdfc","mid-cap","opportunities"],["quant","mid","cap"],["edelweiss","mid","cap"],["nippon","growth mid cap","plan growth"]],
 "Small Cap":[["nippon","small","cap"],["quant","small","cap"],["invesco","smallcap"],["hdfc","small","cap"],["bandhan","small","cap"],["tata","small","cap"],["sbi","small","cap"]],
 "ELSS":[["quant","elss"],["hdfc","elss"],["mirae","tax","saver"],["parag","parikh","elss"]],
 "Hybrid Aggressive":[["icici","equity","debt"],["hdfc","hybrid","equity"],["quant","absolute"],["sbi","equity","hybrid"]],
 "Index":[["nippon","nifty","smallcap","250","index"],["uti","nifty","50","index"],["motilal","nifty","midcap","150"]],
}
funds=[]; seen=set()
for name,isin,cat,units,avgn in MF_HOLDINGS:
    code=isin2code.get(isin)
    if not code: continue
    try: m=fund_metrics(code,name,cat); m['held']=True; funds.append(m); seen.add(code)
    except Exception: pass
for cat,lst in CURATED.items():
    for kw in lst:
        code=find_code(kw)
        if not code or code in seen: continue
        seen.add(code)
        try: m=fund_metrics(code,code2name[code],cat); m['held']=False; funds.append(m)
        except Exception: pass
print(f"funds: {len(funds)} ({sum(f['held'] for f in funds)} held)")
if len(funds)-sum(f['held'] for f in funds)==0:
    print("WARNING: no curated comparison funds matched - AMFI name format may have changed")
fd=pd.DataFrame(funds); fd['rank_metric']=fd['r1y'].fillna(fd['r6m'])
fd['cat_pctile']=fd.groupby('cat')['rank_metric'].rank(pct=True)*100
def packf(df):
    out=[]
    for _,x in df.sort_values(['cat','rank_metric'],ascending=[True,False]).iterrows():
        g=lambda v: round(v*100,1) if v==v else None
        nm=x['name'] if x['held'] else re.sub(r'\s+(Fund\s+)?Direct Plan.*$','',x['name'],flags=re.I).replace(' Fund',' ')
        nm=re.sub(r'\s+',' ',nm).strip()
        out.append(dict(name=nm[:46],cat=x['cat'],nav=x['nav'],r1m=g(x['r1m']),r3m=g(x['r3m']),
            r6m=g(x['r6m']),r1y=g(x['r1y']),r3y=g(x['r3y']),r5y=g(x['r5y']),
            sharpe=round(x['sharpe'],2) if x['sharpe']==x['sharpe'] else None,
            maxdd=round(x['maxdd']*100) if x['maxdd']==x['maxdd'] else None,
            pctile=int(x['cat_pctile']) if x['cat_pctile']==x['cat_pctile'] else None,held=bool(x['held'])))
    return out
funds_all=packf(fd)

# ----------------------------------------------------------------------------- 4. my portfolio
dmap={r['sym']:r for r in stocks_all}
my_stocks=[]; invested_s=present_s=0.0
for sym,sec,qty,avg,ltq in STOCK_HOLDINGS:
    r=dmap.get(sym); px=r['px'] if r else avg
    inv=qty*avg; pres=qty*px; invested_s+=inv; present_s+=pres
    rep=None
    if r and (r['tag']=="AVOID" or r['pctile']<35):
        same=[x for x in stocks_all if x['sector']==sec and not x['held'] and x['tag'] in ("BUY-WATCH","EXTENDED")]
        pool=same or [x for x in stocks_all if not x['held'] and x['tag']=="BUY-WATCH"]
        if pool: rep=pool[0]['sym']
    if not r: read="NO DATA"
    elif r['pctile']>=60 and r['tag'] in("BUY-WATCH","EXTENDED"): read="KEEP"
    elif r['pctile']<35 or r['tag']=="AVOID": read="TRIM/REPLACE"
    else: read="HOLD"
    r1d=(r['r1d'] if r else 0) or 0
    day_pl=pres-pres/(1+r1d/100) if r1d>-100 else 0
    ltq=min(ltq,qty); lt_pl=(pres-inv)*(ltq/qty) if qty else 0
    my_stocks.append(dict(sym=sym,sector=sec,qty=qty,avg=round(avg,2),px=px,inv=round(inv),pres=round(pres),
        r1d=r1d,day_pl=round(day_pl),ltq=ltq,lt_pl=round(lt_pl),st_pl=round((pres-inv)-lt_pl),
        pl=round(pres-inv),plpct=round((px/avg-1)*100,1),wt=0,score=r['score'] if r else None,
        pctile=r['pctile'] if r else None,tag=r['tag'] if r else "—",read=read,rep=rep))
for m in my_stocks: m['wt']=round(100*m['pres']/present_s,1)
my_stocks.sort(key=lambda x:x['pl'])
# ---- SIP auto-accrual: every instalment after the statement date, priced at its real NAV ----
import calendar
now_ist=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=5,minutes=30)
today_ist=now_ist.date()
asof_d=dt.date.fromisoformat(HOLDINGS_ASOF) if HOLDINGS_ASOF else today_ist
STAMP_DUTY=0.00005   # 0.005% on MF purchases
sip_rows=[]; sip_log=[]; accrual={}
for sp in SIPS:
    code=isin2code.get(sp['isin']); s_nav=None; nav_err=None
    if code:
        try: s_nav=hist(code)
        except Exception as e: nav_err=repr(e)[:80]
    else: nav_err="ISIN not found in AMFI list"
    y,m=asof_d.year,asof_d.month; u_add=a_add=0.0; n=0; pending=[]; nxt=None
    while True:
        d_=dt.date(y,m,min(sp['day'],calendar.monthrange(y,m)[1]))
        if d_>today_ist: nxt=d_; break
        if d_>asof_d:
            w=s_nav[s_nav.index>=pd.Timestamp(d_)] if s_nav is not None else None
            if w is None or w.empty:
                pending.append(d_)          # not priced yet (NAV not out, or history failed) - NOT counted
            else:
                nav_i=float(w.iloc[0]); u=sp['amount']*(1-STAMP_DUTY)/nav_i
                u_add+=u; a_add+=sp['amount']; n+=1
                sip_log.append(dict(date=str(d_),fund=sp['name'],amount=round(sp['amount']),nav=round(nav_i,4),
                                    nav_date=str(w.index[0].date()),units=round(u,3)))
        m+=1
        if m>12: m=1; y+=1
    accrual[sp['isin']]=(u_add,a_add)
    sip_rows.append(dict(name=sp['name'],isin=sp['isin'],amount=round(sp['amount']),day=sp['day'],
        next=str(nxt),pending=[str(x) for x in pending],n_added=n,units_added=round(u_add,3),
        amount_added=round(a_add),nav_error=nav_err))
    print(f"SIP {sp['name']}: +{n} instalments, +{u_add:.3f} units, pending {len(pending)}"+(f", NAV ERROR {nav_err}" if nav_err else ""))
sip_log.sort(key=lambda x:x['date'],reverse=True)
held_isins={x[1] for x in MF_HOLDINGS}
extra=[(sp['name'],sp['isin'],"SIP (new fund)",0.0,0.0) for sp in SIPS if sp['isin'] not in held_isins and accrual.get(sp['isin'],(0,0))[0]>0]

hmap={f['name']:f for f in funds_all}; my_mf=[]; invested_m=present_m=0.0
for name,isin,cat,units,avgn in list(MF_HOLDINGS)+extra:
    code=isin2code.get(isin); rec=next((f for f in funds if f['code']==code),None) if code else None
    u_add,a_add=accrual.get(isin,(0.0,0.0))
    inv=units*avgn+a_add; units=units+u_add; avgn=inv/units if units else avgn
    nav=rec['nav'] if rec else avgn; pres=units*nav; invested_m+=inv; present_m+=pres
    day_pl=units*(nav-rec['nav_prev']) if rec else 0.0
    fa=hmap.get(name[:46]); pctile=fa['pctile'] if fa else None
    read="—" if pctile is None else ("KEEP" if pctile>=60 else ("REVIEW" if pctile<35 else "HOLD"))
    my_mf.append(dict(name=name,cat=cat,units=round(units,3),avg=round(avgn,4),nav=round(nav,4),inv=round(inv),
        day_pl=round(day_pl),nav_date=rec['nav_date'] if rec else None,nav_prev_date=rec['nav_prev_date'] if rec else None,sip_units=round(u_add,3),sip_amount=round(a_add),
        pres=round(pres),pl=round(pres-inv),plpct=round((pres/inv-1)*100,1) if inv else 0,wt=0,
        r1y=fa['r1y'] if fa else None,pctile=pctile,read=read))
for m in my_mf: m['wt']=round(100*m['pres']/present_m,1)
my_mf.sort(key=lambda x:x['pl'])
sector_w={}
for m in my_stocks: sector_w[m['sector']]=sector_w.get(m['sector'],0)+m['pres']
sector_w={k:round(100*v/present_s,1) for k,v in sorted(sector_w.items(),key=lambda x:-x[1])}
mf_cat_w={}
for m in my_mf: mf_cat_w[m['cat']]=mf_cat_w.get(m['cat'],0)+m['pres']
mf_cat_w={k:round(100*v/present_m,1) for k,v in sorted(mf_cat_w.items(),key=lambda x:-x[1])}
summary=dict(
    s_inv=round(invested_s),s_pres=round(present_s),s_pl=round(present_s-invested_s),s_plpct=round(100*(present_s/invested_s-1),2),
    m_inv=round(invested_m),m_pres=round(present_m),m_pl=round(present_m-invested_m),m_plpct=round(100*(present_m/invested_m-1),2),
    t_inv=round(invested_s+invested_m),t_pres=round(present_s+present_m),t_pl=round(present_s+present_m-invested_s-invested_m),
    t_plpct=round(100*((present_s+present_m)/(invested_s+invested_m)-1),2),
    n_up=int((d.a50&d.a200&(d.adx>20)).sum()),n_screened=len(d),
    breadth=round(100*(d.a50&d.a200&(d.adx>20)).sum()/len(d)),top=stocks_all[0]['sym'],last_bar=last_bar,
    asof=str(dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=5,minutes=30))).strftime("%Y-%m-%d %H:%M IST")),
    built_utc=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    holdings_asof=HOLDINGS_ASOF,
    nav_prev_date=min([m['nav_prev_date'] for m in my_mf if m.get('nav_prev_date')] or [None]) if any(m.get('nav_prev_date') for m in my_mf) else None,
    nav_date=max([m['nav_date'] for m in my_mf if m['nav_date']] or [None]) if any(m['nav_date'] for m in my_mf) else None,
    day_pl=round(sum(m['day_pl'] for m in my_stocks)+sum(m['day_pl'] for m in my_mf)),
    s_day_pl=round(sum(m['day_pl'] for m in my_stocks)), m_day_pl=round(sum(m['day_pl'] for m in my_mf)),
    sip_monthly=round(sum(sp['amount'] for sp in SIPS)), sip_count=len(SIPS),
    sip_added=round(sum(r['amount_added'] for r in sip_rows)), sip_n_added=sum(r['n_added'] for r in sip_rows),
    sip_pending=sum(len(r['pending']) for r in sip_rows),
    lt_loss=round(sum(min(m['lt_pl'],0) for m in my_stocks)), st_loss=round(sum(min(m['st_pl'],0) for m in my_stocks)))
payload=dict(summary=summary,stocks_all=stocks_all,top_day=top_day,top_week=top_week,top_month=top_month,
    stable=stable,funds_all=funds_all,my_stocks=my_stocks,my_mf=my_mf,sector_w=sector_w,mf_cat_w=mf_cat_w,
    sips=sip_rows,sip_log=sip_log)

# ----------------------------------------------------------------------------- 5. encrypt + write
if not os.environ.get("GITHUB_ACTIONS"):
    with open(CACHE,"w",encoding="utf-8") as f: json.dump(payload,f)   # gitignored; local re-render only
write_encrypted(payload)
print(f"Stocks {summary['s_plpct']}% | MF {summary['m_plpct']}% | Total {summary['t_plpct']}% | breadth {summary['breadth']}%")
