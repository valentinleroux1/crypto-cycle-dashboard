#!/usr/bin/env python3
"""
Dashboard "sommet de cycle" — moteur de score composite.
Récupère 6 signaux (best-effort scrape gratuit), calcule un score pondéré 0-100
de risque de sommet, et régénère la page HTML.

Seuils ADAPTATIFS : les niveaux "sommet" baissent chaque cycle (rendements
décroissants). Réglés pour la fenêtre 2029.

Usage:  python score.py [chemin_sortie.html]
"""
import ssl, json, urllib.request, io, csv, sys, os, datetime

# --- réseau : TLS vérifié, avec bascule non vérifié si le certificat paraît
#     expiré/invalide (robuste aux environnements à horloge décalée). ---
import time
def _unverified_ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c

def _fetch(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()).read()
    except ssl.SSLError:
        return urllib.request.urlopen(req, timeout=timeout, context=_unverified_ctx()).read()

def _open(url, timeout, accept=None, attempts=3):
    h = {"User-Agent": "Mozilla/5.0"}
    if accept:
        h["Accept"] = accept
    req = urllib.request.Request(url, headers=h)
    last = None
    for attempt in range(attempts):
        try:
            return _fetch(req, timeout)
        except Exception as e:
            last = e
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1))   # backoff 3s, 6s
    raise last

def get_json(url, timeout=25, attempts=3):
    return json.loads(_open(url, timeout, accept="application/json", attempts=attempts))

def get_text(url, timeout=15, attempts=1):
    return _open(url, timeout, attempts=attempts).decode()

# --- état persistant (dernières valeurs connues + total ETF précédent) ---
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

state = load_state()
TODAY_ISO = datetime.date.today().isoformat()

def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))

# ======================= RÉCUPÉRATION DES SIGNAUX =======================
notes = {}

# 1) Pi Cycle Top — Binance klines quotidiennes
def sig_pi():
    d = get_json("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=400")
    closes = [float(c[4]) for c in d]
    sma111 = sum(closes[-111:]) / 111
    sma350 = sum(closes[-350:]) / 350
    ratio = sma111 / (2 * sma350)          # =1.0 => croisement (sommet)
    score = clamp((ratio - 0.30) / (1.00 - 0.30) * 100)
    return score, f"111DMA / 2×350DMA = {ratio:.2f}", {"ratio": ratio}

# 2) Valorisation — MVRV Z-score + NUPL (bitcoin-data.com)
def sig_val():
    z = float(get_json("https://bitcoin-data.com/v1/mvrv-zscore/last")["mvrvZscore"])
    nupl = float(get_json("https://bitcoin-data.com/v1/nupl/last")["nupl"])
    s_mvrv = clamp(z / 5.0 * 100)          # seuil sommet adaptatif 2029 : Z~5
    s_nupl = clamp(nupl / 0.75 * 100)      # euphorie ~0.75
    return (s_mvrv + s_nupl) / 2, f"MVRV Z {z:.2f} · NUPL {nupl:.2f}", {"mvrv": z, "nupl": nupl}

# 3) Rotation — dominance BTC + ETH/BTC (CoinGecko)
def sig_rot():
    g = get_json("https://api.coingecko.com/api/v3/global")["data"]
    dom = g["market_cap_percentage"]["btc"]
    p = get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd")
    ethbtc = p["ethereum"]["usd"] / p["bitcoin"]["usd"]
    s_dom = clamp((60 - dom) / (60 - 45) * 100)      # dominance qui chute = rotation tardive
    s_eb = clamp((ethbtc - 0.030) / (0.060 - 0.030) * 100)
    return (s_dom + s_eb) / 2, f"Dom. BTC {dom:.1f}% · ETH/BTC {ethbtc:.4f}", {"dom": dom, "ethbtc": ethbtc}

# 4) Froth — funding Binance + Fear & Greed
def sig_froth():
    fr = float(get_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")["lastFundingRate"])
    fg = int(get_json("https://api.alternative.me/fng/?limit=1")["data"][0]["value"])
    s_fund = clamp(fr / 0.0010 * 100)                # funding chaud ~0.10%/8h
    s_fg = clamp((fg - 25) / (90 - 25) * 100)
    return (s_fund + s_fg) / 2, f"Funding {fr*100:.3f}% · F&G {fg}", {"funding": fr, "fng": fg}

# 5) Flux ETF — variation hebdo du total BTC en ETF (bitcoin-data.com)
def sig_etf():
    total = float(get_json("https://bitcoin-data.com/v1/etf-btc-total/last")["etfBtcTotal"])
    prev = state.get("etf_total")
    state["etf_total"] = total
    if prev is None:
        return 30.0, f"Total ETF {total:,.0f} BTC (1re mesure)", {"etf_total": total}
    delta = total - prev
    # accumulation (delta>0) = demande présente = FAIBLE risque de sommet ;
    # distribution (delta<0) au prix haut = risque élevé.
    pct = delta / prev * 100
    score = clamp(50 - pct * 40)                     # +1% => 10 ; -1% => 90
    sens = "accumulation" if delta >= 0 else "distribution"
    return score, f"Total ETF {total:,.0f} BTC · {sens} ({pct:+.2f}%/sem)", {"etf_total": total}

# 6) Liquidité M2 — FRED CSV (best-effort, fallback dernière valeur)
def sig_m2():
    raw = get_text("https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL", timeout=45)
    rows = [r for r in csv.reader(io.StringIO(raw)) if r and r[0] != "DATE"]
    m2, m2y = float(rows[-1][1]), float(rows[-13][1])
    yoy = (m2 / m2y - 1) * 100
    state["m2_yoy"] = yoy
    # expansion forte = carburant présent = risque bas ; contraction = risque haut
    score = clamp(50 - yoy * 6)                       # +5% YoY => 20 ; -3% => 68
    return score, f"M2 YoY {yoy:+.1f}%", {"m2_yoy": yoy}

# poids (somme = 1.0)
SIGNALS = [
    ("Valorisation",     "à quel point c'est tendu", 0.25, sig_val),
    ("Liquidité globale","le carburant",             0.20, sig_m2),
    ("Flux ETF",         "la demande",               0.20, sig_etf),
    ("Rotation",         "le régime",                0.15, sig_rot),
    ("Pi Cycle Top",     "le timing",                0.10, sig_pi),
    ("Froth",            "le sentiment / levier",    0.10, sig_froth),
]

results = []
for name, q, w, fn in SIGNALS:
    key = "last_" + name
    try:
        score, val, _ = fn()
        state[key] = {"score": score, "val": val, "date": TODAY_ISO}
        stale = False
    except Exception as e:
        cached = state.get(key)
        if cached:
            score = cached["score"]
            same_day = cached.get("date") == TODAY_ISO
            val = cached["val"] if same_day else cached["val"] + f" (valeur du {cached.get('date','?')})"
            stale = not same_day
        else:
            score, val, stale = 40.0, "indisponible", True
        notes[name] = repr(e)[:80]
    results.append({"name": name, "q": q, "w": w, "score": score, "val": val, "stale": stale})

composite = round(sum(r["score"] * r["w"] for r in results))

# ======================= RENDU HTML =======================
def band(s):
    if s < 30:  return ("var(--ok)",   "Bas")
    if s < 50:  return ("var(--watch)","Neutre")
    if s < 70:  return ("var(--warn)", "Vigilance")
    if s < 85:  return ("var(--alert)","Distribution")
    return ("var(--crit)", "Sommet")

def verdict(s):
    if s < 30:  return ("var(--ok)",   "Zone d'accumulation", "Zéro alerte sommet. Rien à faire côté vente.")
    if s < 50:  return ("var(--watch)","Mi-cycle",            "Surveiller, poser les alertes.")
    if s < 70:  return ("var(--warn)", "Vigilance",           "Armer les ordres, déclencher le bas des ladders.")
    if s < 85:  return ("var(--alert)","Distribution",        "Accélérer les ladders — vendre dans la force.")
    return ("var(--crit)", "Sommet probable", "Backstop temporel : vendre le reste au marché.")

vcol, vlab, vnote = verdict(composite)
today = datetime.date.today().strftime("%d/%m/%Y")

# --- détection de franchissement de palier (avec hystérésis ±2) ---
PALIERS = ["ACCUMULATION", "MI-CYCLE", "VIGILANCE", "DISTRIBUTION", "SOMMET"]
ACTIONS = {
    0: "Rien côté vente — on accumule.",
    1: "Surveiller, poser les alertes.",
    2: "Armer les ordres limites, déclencher le bas des ladders.",
    3: "Accélérer les ladders — vendre dans la force.",
    4: "Backstop temporel : vendre le reste au marché.",
}
def band_idx(score, prev):
    ups, H = [30, 50, 70, 85], 2
    idx = prev if prev is not None else 0
    while idx < 4 and score >= ups[idx] + H:
        idx += 1
    while idx > 0 and score < ups[idx - 1] - H:
        idx -= 1
    return idx

prev_idx = state.get("last_band_idx")
cur_idx = band_idx(composite, prev_idx)
changed = prev_idx is not None and cur_idx != prev_idx
arrow = "↑" if (prev_idx is not None and cur_idx > prev_idx) else "↓"
state["last_band_idx"] = cur_idx
state["last_band"] = PALIERS[cur_idx]

alert = {"changed": bool(changed)}
if changed:
    lines = [f"**Palier franchi : {PALIERS[cur_idx]}** — score **{composite}/100** ({arrow} depuis {PALIERS[prev_idx]}).",
             "", f"**Action : {ACTIONS[cur_idx]}**", "", "| Signal | Valeur | Sous-score |", "|---|---|---|"]
    lines += [f"| {r['name']} | {r['val']} | {r['score']:.0f} |" for r in results]
    lines += ["", "_Alerte automatique — dashboard sommet de cycle 2029._"]
    alert["title"] = f"[Cycle 2029] Palier {arrow} : {PALIERS[cur_idx]} (score {composite}/100)"
    alert["body"] = "\n".join(lines)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.json"), "w", encoding="utf-8") as f:
    json.dump(alert, f, ensure_ascii=False, indent=2)

cards = ""
for r in results:
    scol, pill = band(r["score"])
    cards += f"""    <div class="card" style="--s:{scol}">
      <div class="chead"><div><div class="nm">{r['name']}</div><div class="q">{r['q']}</div></div><span class="pill">{pill}</span></div>
      <div class="val">{r['val']}</div>
      <div class="sbar"><div class="f" style="width:{r['score']:.0f}%"></div></div>
      <div class="foot"><span>sous-score <b>{r['score']:.0f}</b>/100</span><span>poids {r['w']*100:.0f} %</span></div>
    </div>
"""

TEMPLATE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html"), "r", encoding="utf-8").read()
html = (TEMPLATE
        .replace("__SCORE__", str(composite))
        .replace("__VCOL__", vcol)
        .replace("__VLAB__", vlab)
        .replace("__VNOTE__", vnote)
        .replace("__MARKER__", str(composite))
        .replace("__CARDS__", cards)
        .replace("__UPDATED__", today))

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard-sommet-cycle.html")
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
save_state(state)

print(f"Score composite : {composite}/100  ({vlab})")
for r in results:
    flag = "  [STALE]" if r["stale"] else ""
    print(f"  {r['name']:<18} {r['score']:5.1f}  x{r['w']:.2f}   {r['val']}{flag}")
if notes:
    print("Signaux en fallback :", notes)
print(f"Palier : {PALIERS[cur_idx]}" + (f"  <<< FRANCHISSEMENT {arrow} (alerte)" if changed else "  (inchangé)"))
print("HTML écrit :", out)
