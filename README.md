# DuringDays → Kogan & BigW Sitemap Matcher

Match each DuringDays product to the **exact variant** URL on Kogan and BigW using
only public XML sitemaps (no per-product search scraping), then optionally verify
confirmed matches via JSON-LD on the product page.

## Setup

```powershell
cd kogan-bigw-matcher
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Place your input spreadsheet at:

```
data/products.xlsx
```

Sheet name must be **Products** with the 16 columns described in the project spec
(ID, Title, Vendor, …, Kogan Title/URL/Price, BigW Title/URL/Price, Validation).

## Web app (URL Matcher Studio)

```powershell
cd kogan-bigw-matcher
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python serve.py
```

Open **http://127.0.0.1:8787**

Features:
- Upload Excel → auto-adds missing `BigW Name` / `BigW URL` / `BigW Match` columns
- Row range picker clamped to real sheet bounds
- Live progress (SSE) while matching
- Preview matched rows + download result
- Dashboard of jobs / indexed URL counts
- Custom sitemap crawl (stores product URL catalogues for matching)

## CLI (still available)

Default input: `data/Final_DD_URL.xlsx` (in-place by default). Columns auto-detected; BigW Name filled from URL slug unless `--fetch-names`.

```powershell
python run.py --site bigw --start-row 2 --end-row 50
python run.py --site bigw --start-row 2 --end-row 50 --fetch-names
python run.py --site bigw --start-row 2 --end-row 50 --verify
```

## Railway deployment

This service is a **Python FastAPI** app (no Node.js build step).

### Deploy on Railway

1. Create a new Railway project from this repo (root: `kogan-bigw-matcher` or repo root if monorepo).
2. Railway detects `Dockerfile` / `railway.json` and builds automatically.
3. Set a **persistent volume** (recommended) mounted at:
   - `/app/cache` — sitemap URL catalogues (~90MB+ for BigW)
   - `/app/uploads` — uploaded workbooks
   - `/app/outputs` — matched results & job state
4. Health check: `GET /api/health`

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PORT` | Set by Railway | `8787` | HTTP port (Railway injects this) |
| `HOST` | No | `0.0.0.0` when `PORT` set | Bind address |
| `FORWARDED_ALLOW_IPS` | No | `*` | Trusted proxy IPs for uvicorn |

Copy `.env.example` for local Docker/production testing.

### Local production test

```powershell
docker compose up --build
# open http://localhost:8787
```

Or without Docker:

```powershell
$env:PORT="8787"; python serve.py
```

## Staged recipe

1. **Stage 1 – sitemaps** (biggest unknown):  
   `python -c "import sitemaps; sitemaps.crawl_site('bigw'); sitemaps.crawl_site('kogan')"`  
   Confirm product-URL counts print and `cache/*.json` are written.
2. **Stage 2 – index + query parse**: automatic on first `run.py`; also  
   `python product_query.py` for a self-test of title parsing.
3. **Stage 3 – match pilot**: `python run.py --limit 20 --no-verify` → review `run_log.csv`.
4. **Stage 4 – verify**: `python run.py --limit 20` → tune thresholds → full run.

## Scoring weights & thresholds (`config.py`)

| Constant | Default | Meaning / how to tune |
|----------|---------|------------------------|
| `W_VARIANT_ATTR` | 25 | Points per colour/size/volume/etc. found in the slug. Raise if variants are under-weighted vs fuzzy noise. |
| `W_MODEL_TOKEN` | 8 | Points per model/descriptor token hit. Raise if brand+attrs alone aren't enough to surface the right product. |
| `W_FUZZY` | 0.4 | Multiplier on rapidfuzz `token_sort_ratio` (0–100). Tie-breaker; lower if fuzzy is overpowering hard attrs. |
| `MATCH_THRESHOLD` | 55 | Score ≥ this **and** all variant attrs present → `MATCH`. Too many false MATCHes → **raise**; missing valid matches → **lower**. |
| `PARTIAL_THRESHOLD` | 30 | Score ≥ this but attrs missing → `PARTIAL`. |
| `WRITE_PARTIAL_URL` | False | If True, write best-guess URL into the URL column for PARTIALs (default keeps `-`, URL only in run_log). |
| `VERIFY_FUZZY_MIN` | 70 | JSON-LD name must fuzzy-match the title at least this well or the row is downgraded to PARTIAL. |
| `VERIFY_DELAY_SEC` | 1.5 | Polite delay between verification page fetches. Increase if Kogan/BigW start returning non-200. |
| `KNOWN_BRANDS` | set | Seed brand tokens. Add missing house brands if leading-token detection fails. |
| `COLOURS` / `SIZES` | sets | Extend when new variant vocab appears in titles/slugs. |

## Blocking / politeness notes

- **Sitemap stage is the safe bulk path.** BigW works over HTTPS with a browser-like TLS fingerprint (`curl_cffi`).
- **Kogan** sits behind DataDome: plain HTTP gets 403. The crawler falls back to a headed Edge/Chrome session and fetches child sitemaps in-page. This is slower and rate-limited; progress is checkpointed to `cache/kogan_urls.json` every chunk. Expect a long first crawl.
- **Verification** makes **one polite request per confirmed MATCH** only. If pages start failing, raise `VERIFY_DELAY_SEC` or run with `--no-verify`.

## Column write rules

Retailer **URL** is written only on `MATCH` (unless `WRITE_PARTIAL_URL=True`).  
**Title** / **Price** are filled from JSON-LD during verification; otherwise `-`.  
**Validation** combines both sites, e.g.  
`Kogan=MATCH: … | BigW=PARTIAL: variant unclear (500ml not found)`.
