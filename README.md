# Dashboard « sommet de cycle »

Indicateur composite pondéré (0-100) du **risque de sommet de cycle** crypto, recalculé
automatiquement chaque semaine par GitHub Actions et publié sur GitHub Pages.

## Ce que ça fait

`score.py` récupère 6 signaux (scrape gratuit), les normalise avec des **seuils adaptatifs**
(qui baissent chaque cycle pour intégrer les rendements décroissants), calcule un score
pondéré, et régénère `template.html` → `index.html`.

| Signal | Poids | Source |
|---|---|---|
| Valorisation (MVRV Z + NUPL) | 25 % | bitcoin-data.com |
| Liquidité globale (M2 YoY) | 20 % | FRED (CSV) |
| Flux ETF (delta hebdo) | 20 % | bitcoin-data.com |
| Rotation (dominance BTC + ETH/BTC) | 15 % | CoinGecko |
| Pi Cycle Top | 10 % | Binance (klines) |
| Froth (funding + Fear & Greed) | 10 % | Binance + alternative.me |

**Score → action** : 0-30 accumulation · 30-50 mi-cycle · 50-70 vigilance (armer les ordres) ·
70-85 distribution (accélérer les ladders) · 85-100 sommet (backstop, tout vendre).

Un signal indisponible retombe sur sa dernière valeur connue (`state.json`).

## Installation (une fois, ~10 min)

1. **Crée un nouveau repo GitHub** (public — Pages est gratuit sur public).
2. **Pousse ces 4 fichiers** à la racine du repo :
   - `score.py`
   - `template.html`
   - `state.json`   (état de départ — sert de base au 1er delta ETF)
   - `.github/workflows/dashboard.yml`
3. **Settings → Pages → Build and deployment → Source : GitHub Actions**.
4. **Settings → Actions → General → Workflow permissions : Read and write permissions** (pour que le workflow puisse committer `state.json`).
5. **Onglet Actions → « Dashboard sommet de cycle » → Run workflow** (premier lancement manuel).
6. Ton dashboard est à : **`https://<ton-pseudo>.github.io/<nom-du-repo>/`**

Ensuite, plus rien à faire : ça tourne **chaque lundi 12:00 UTC**, tout seul, gratuitement.
(Le commit hebdo de `state.json` garde aussi le cron actif — pas de désactivation après 60 j.)

## Alertes (email via GitHub Issue)

À chaque run, le script compare le **palier courant** au précédent (stocké dans `state.json`).
S'il y a **franchissement** — avec une hystérésis de ±2 points pour éviter le flapping autour
d'une frontière — le workflow ouvre une **issue GitHub**, ce qui déclenche l'email de notification
GitHub (+ notif de l'appli mobile).

Rare par nature (signal multi-années) → tu n'es notifié que quand ça compte vraiment.
Nécessite `issues: write` dans les permissions (déjà dans le workflow) et rien d'autre.

**Deux types d'alertes :**
1. **Palier du score composite** (ACCUMULATION → … → SOMMET) : le *timing* global.
2. **Palier de prix du ladder** : dès que BTC/ETH/SOL atteint un niveau de vente du plan de
   sortie (ex. « BTC a atteint 170 000$ → vendre 30% »). Chaque niveau n'alerte **qu'une fois**
   (mémorisé dans `state.json → ladder_hits`). Niveaux définis dans `LADDERS` en tête de `score.py`.

## Lancer en local (optionnel)

```
python score.py index.html
```

Écrit `index.html` + met à jour `state.json`. (En environnement à horloge décalée, le script
bascule automatiquement sur un contexte TLS non vérifié.)

## Réglages

- **Cadence** : modifie la ligne `cron:` dans `dashboard.yml` (ex. `"0 12 * * *"` = quotidien).
- **Poids / seuils adaptatifs** : en tête de `score.py` (liste `SIGNALS` + fonctions `sig_*`).
