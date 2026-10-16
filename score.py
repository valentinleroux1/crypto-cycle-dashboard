#!/usr/bin/env python3
"""
Dashboard "sommet de cycle" — moteur de score composite.
Récupère 6 signaux (best-effort scrape gratuit), calcule un score pondéré 0-100
de risque de sommet, et régénère la page HTML.

Seuils ADAPTATIFS : les niveaux "sommet" baissent chaque cycle (rendements
décroissants). Réglés pour la fenêtre 2029.

Usage:  python score.py [chemin_sortie.html]
"""
import ssl, json, urllib.request, urllib.error, io, csv, sys, os, datetime, statistics

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
    except (ssl.SSLError, urllib.error.URLError) as e:
        # l'erreur SSL de urllib est souvent emballée dans URLError -> on inspecte la cause
        reason = getattr(e, "reason", None)
        blob = str(reason if reason is not None else e).upper()
        if isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError) or "CERTIFICATE" in blob or "SSL" in blob:
            return urllib.request.urlopen(req, timeout=timeout, context=_unverified_ctx()).read()
        raise

def _open(url, timeout, accept=None, attempts=3, ua="Mozilla/5.0"):
    h = {"User-Agent": ua}
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

def get_text(url, timeout=15, attempts=1, ua="Mozilla/5.0"):
    return _open(url, timeout, attempts=attempts, ua=ua).decode()

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
    # CoinGecko (joignable depuis le runner ; Binance y est géo-bloqué)
    d = get_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365")
    closes = [p[1] for p in d["prices"]]
    sma111 = sum(closes[-111:]) / 111
    sma350 = sum(closes[-350:]) / 350
    ratio = sma111 / (2 * sma350)          # =1.0 => croisement (sommet)
    state["pi_ratio"] = ratio               # exposé pour l'override
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

# 4) Froth — Fear & Greed (funding Binance abandonné : géo-bloqué sur le runner)
def sig_froth():
    fg = int(get_json("https://api.alternative.me/fng/?limit=1")["data"][0]["value"])
    score = clamp((fg - 25) / (90 - 25) * 100)
    return score, f"Fear & Greed {fg}", {"fng": fg}

# 5) Flux ETF — flux hebdo du total BTC en ETF, noté vs sa PROPRE norme récente
#    (seuil ADAPTATIF : pas de dérive quand l'adoption fait monter la base).
def sig_etf():
    total = float(get_json("https://bitcoin-data.com/v1/etf-btc-total/last")["etfBtcTotal"])
    prev = state.get("etf_total")
    hist = [h for h in state.setdefault("etf_hist", []) if h != 0.0]   # purge des deltas nuls parasites
    state["etf_hist"] = hist
    last_d = state.get("etf_date") or state.get("last_Flux ETF", {}).get("date")
    if prev is None:
        state["etf_total"], state["etf_date"] = total, TODAY_ISO
        return 45.0, f"Total ETF {total:,.0f} BTC (init)", {}
    if last_d and (datetime.date.today() - datetime.date.fromisoformat(last_d)).days < 5:
        raise RuntimeError("relance < 5 j : delta ETF non hebdo, on garde la valeur de la semaine")
    delta = total - prev
    state["etf_total"], state["etf_date"] = total, TODAY_ISO
    hist.append(delta)
    del hist[:-12]                                    # fenêtre glissante ~12 semaines
    sens = "accumulation" if delta >= 0 else "distribution"
    n = len(hist)
    if n < 5:
        return 45.0, f"Flux {sens} · calibrage {n}/5", {}
    mean = statistics.fmean(hist)
    sd = statistics.pstdev(hist) or 1.0
    z = (delta - mean) / sd                           # flux courant vs norme récente
    score = clamp(45 - z * 22)                        # fort inflow => bas ; outflow/faible => haut
    return score, f"Flux {sens} vs norme récente (z={z:+.1f})", {"z": z}

# 6) Liquidité M2 — plusieurs sources, la première qui répond gagne.
#    Le CSV fredgraph n'a jamais répondu depuis les runners GitHub (Akamai) :
#    l'API officielle FRED (clé gratuite, secret FRED_API_KEY) passe en premier si dispo.
MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
def _m2_fred_api():
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        raise RuntimeError("pas de FRED_API_KEY")
    d = get_json("https://api.stlouisfed.org/fred/series/observations?series_id=M2SL"
                 f"&api_key={key}&file_type=json&sort_order=desc&limit=13", timeout=20, attempts=2)
    obs = [(o["date"], float(o["value"])) for o in d["observations"] if o["value"] not in (".", "")]
    return list(reversed(obs))
def _m2_fred_csv():
    raw = get_text("https://fred.stlouisfed.org/graph/fredgraph.csv?id=M2SL", timeout=20,
                   attempts=2, ua="crypto-cycle-dashboard/1.0 (python-urllib)")
    rows = []
    for r in csv.reader(io.StringIO(raw)):
        try:
            rows.append((r[0], float(r[1])))
        except (IndexError, ValueError):
            pass
    return rows
M2_SOURCES = [("FRED API", _m2_fred_api), ("FRED CSV", _m2_fred_csv)]

def sig_m2():
    errs = []
    for label, fn in M2_SOURCES:
        try:
            rows = fn()
            if len(rows) < 13:
                raise RuntimeError(f"{len(rows)} lignes")
            break
        except Exception as e:
            errs.append(f"{label}: {e!r}"[:120])
    else:
        raise RuntimeError(" | ".join(errs))
    print(f"  M2 via {label}" + (f" (après échecs : {errs})" if errs else ""))
    (d_last, m2), m2y = rows[-1], rows[-13][1]
    yoy = (m2 / m2y - 1) * 100
    state["m2_yoy"] = yoy
    y, mo = int(d_last[:4]), int(d_last[5:7])
    # expansion forte = carburant présent = risque bas ; contraction = risque haut
    score = clamp(50 - yoy * 6)                       # +5% YoY => 20 ; -3% => 68
    return score, f"M2 YoY {yoy:+.1f}% ({MOIS[mo-1]} {y})", {"m2_yoy": yoy}

# poids (somme = 1.0)
SIGNALS = [
    ("Valorisation",     "à quel point c'est tendu", 0.25, sig_val),
    ("Liquidité globale","le carburant",             0.20, sig_m2),
    ("Flux ETF",         "la demande",               0.20, sig_etf),
    ("Rotation",         "le régime",                0.15, sig_rot),
    ("Pi Cycle Top",     "le timing",                0.10, sig_pi),
    ("Froth",            "le sentiment",             0.10, sig_froth),
]

# Délai DUR par signal : un serveur qui « laisse pendre » la connexion (cas du run du
# 05/10/2026, tué après 15 min) ne peut plus bloquer tout le workflow.
import threading
SIGNAL_DEADLINE = 120                     # secondes, toutes tentatives comprises
# Au-delà de cet âge, une valeur de repli n'est plus de l'information : le signal sort
# du score et les poids des autres sont renormalisés.
MAX_AGE_DAYS = {"Liquidité globale": 45}  # M2 mensuel : 45 j sans succès = au moins une publication ratée
MAX_AGE_DEFAULT = 21

def run_with_deadline(fn, seconds):
    box = {}
    def target():
        try:
            box["res"] = fn()
        except Exception as e:
            box["err"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"délai dur de {seconds}s dépassé")
    if "err" in box:
        raise box["err"]
    return box["res"]

results = []
for name, q, w, fn in SIGNALS:
    key = "last_" + name
    excluded = False
    try:
        score, val, _ = run_with_deadline(fn, SIGNAL_DEADLINE)
        state[key] = {"score": score, "val": val, "date": TODAY_ISO}
        stale = False
    except Exception as e:
        cached = state.get(key)
        if cached:
            score = cached["score"]
            cdate = cached.get("date", "1970-01-01")
            age = (datetime.date.today() - datetime.date.fromisoformat(cdate)).days
            excluded = age > MAX_AGE_DAYS.get(name, MAX_AGE_DEFAULT)
            stale = age > 0
            val = cached["val"] if not stale else cached["val"] + f" (valeur du {cdate}" + (" — exclu du score)" if excluded else ")")
        else:
            score, val, stale, excluded = 40.0, "indisponible — exclu du score", True, True
        notes[name] = repr(e)[:160]
    results.append({"name": name, "q": q, "w": w, "score": score, "val": val,
                    "stale": stale, "excluded": excluded})

# renormalisation des poids sur les signaux retenus
w_kept = sum(r["w"] for r in results if not r["excluded"]) or 1.0
for r in results:
    r["w_eff"] = 0.0 if r["excluded"] else r["w"] / w_kept

composite = round(sum(r["score"] * r["w_eff"] for r in results))

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

alerts = []
if changed:
    lines = [f"**Palier franchi : {PALIERS[cur_idx]}** — score **{composite}/100** ({arrow} depuis {PALIERS[prev_idx]}).",
             "", f"**Action : {ACTIONS[cur_idx]}**", "", "| Signal | Valeur | Sous-score |", "|---|---|---|"]
    lines += [f"| {r['name']} | {r['val']} | {r['score']:.0f} |" for r in results]
    lines += ["", "_Alerte automatique — dashboard sommet de cycle 2029._"]
    alerts.append({"title": f"[Cycle 2029] Palier {arrow} : {PALIERS[cur_idx]} (score {composite}/100)",
                   "body": "\n".join(lines)})

# --- alertes LADDER : franchissement d'un palier de PRIX (niveau de vente) ---
# Paliers alignés sur le plan de sortie 2029. (level $, % à vendre, % cumulé)
LADDERS = {
    "BTC": ("bitcoin",  [(140000, "15%", "15%"), (148000, "12%", "27%"), (155000, "13%", "40%"), (162000, "15%", "55%"), (170000, "15%", "70%"), (182000, "20%", "90%"), (192000, "10%", "100%")]),
    "ETH": ("ethereum", [(4000, "20%", "20%"), (4800, "25%", "45%"), (5200, "13%", "58%"), (5600, "12%", "70%"), (6200, "10%", "80%"), (6800, "8%", "88%"), (8000, "12%", "100%")]),
    "SOL": ("solana",   [(300, "20%", "20%"), (330, "15%", "35%"), (360, "15%", "50%"), (390, "13%", "63%"), (420, "12%", "75%"), (490, "17%", "92%"), (580, "8%", "100%")]),
}
try:
    _ids = ",".join(v[0] for v in LADDERS.values())
    _px = get_json(f"https://api.coingecko.com/api/v3/simple/price?ids={_ids}&vs_currencies=usd")
except Exception:
    _px = {}
hits = state.setdefault("ladder_hits", {})   # "BTC@140000": true  -> déjà alerté
ladder_now = {}
for asset, (cgid, rungs) in LADDERS.items():
    price = _px.get(cgid, {}).get("usd")
    if price is None:
        continue
    ladder_now[asset] = price
    for level, pct, cum in rungs:
        key = f"{asset}@{level}"
        if price >= level and not hits.get(key):
            hits[key] = True
            lvl = f"{level:,}".replace(",", " ")
            prc = f"{price:,.0f}".replace(",", " ")
            alerts.append({
                "title": f"[Ladder] {asset} a atteint {lvl}$ — vendre {pct} (cumul {cum})",
                "body": (f"**{asset} = {prc}$** a atteint le palier de vente **{lvl}$**.\n\n"
                         f"**Action : vendre {pct} de ta ligne {asset}** — cumul **{cum}** sécurisé.\n\n"
                         f"_Alerte automatique — ladder de sortie 2029._"),
            })

# --- OVERRIDE Pi Cycle Top : croisement 111DMA >= 2×350DMA (event, 1 seule fois) ---
pi_ratio = state.get("pi_ratio")
if pi_ratio is not None and pi_ratio >= 1.0 and not state.get("pi_cross_alerted"):
    state["pi_cross_alerted"] = True
    alerts.append({
        "title": f"[OVERRIDE] Pi Cycle Top déclenché — sommet historiquement imminent (ratio {pi_ratio:.2f})",
        "body": ("**Pi Cycle Top vient de se déclencher** : la 111DMA a croisé 2×350DMA "
                 f"(ratio {pi_ratio:.2f} ≥ 1,0).\n\n"
                 "Cet indicateur a pointé les sommets **2013 et 2017 à quelques jours près** "
                 "(⚠️ raté partiel en 2021 : avril vs novembre — cycle distordu par le COVID).\n\n"
                 "**Action : override → passage forcé en DISTRIBUTION.** Accélérer les ladders / "
                 "vendre agressivement, quel que soit le score composite.\n\n"
                 "À croiser avec la confluence (M2, MVRV, funding…) — c'est un signal fort, pas une "
                 "garantie.\n\n_Alerte override — dashboard sommet de cycle 2029._"),
    })

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert.json"), "w", encoding="utf-8") as f:
    json.dump({"alerts": alerts}, f, ensure_ascii=False, indent=2)

cards = ""
for r in results:
    scol, pill = band(r["score"])
    cards += f"""    <div class="card" style="--s:{scol}">
      <div class="chead"><div><div class="nm">{r['name']}</div><div class="q">{r['q']}</div></div><span class="pill">{pill}</span></div>
      <div class="val">{r['val']}</div>
      <div class="sbar"><div class="f" style="width:{r['score']:.0f}%"></div></div>
      <div class="foot"><span>sous-score <b>{r['score']:.0f}</b>/100</span><span>{"exclu (donnée périmée)" if r["excluded"] else f"poids {r['w_eff']*100:.0f} %"}</span></div>
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
    flag += "  [EXCLU]" if r["excluded"] else ""
    print(f"  {r['name']:<18} {r['score']:5.1f}  x{r['w_eff']:.2f}   {r['val']}{flag}")
if notes:
    print("Signaux en fallback :", notes)
print(f"Palier score : {PALIERS[cur_idx]}" + (f"  <<< FRANCHISSEMENT {arrow}" if changed else "  (inchangé)"))
if ladder_now:
    print("Prix vs 1er palier ladder :", ", ".join(
        f"{a} {p:,.0f}$/{LADDERS[a][1][0][0]:,}$" for a, p in ladder_now.items()))
print(f"Alertes générées ce run : {len(alerts)}")
print("HTML écrit :", out)
