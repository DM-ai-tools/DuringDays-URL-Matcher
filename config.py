from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"
OUTPUT_DIR = ROOT / "output"

INPUT_XLSX = DATA_DIR / "Final_DD_URL.xlsx"
OUTPUT_XLSX = OUTPUT_DIR / "products_matched.xlsx"
UPLOADS_DIR = ROOT / "uploads"
OUTPUTS_DIR = ROOT / "outputs"
JOBS_DIR = OUTPUTS_DIR / "jobs"
RUN_LOG = OUTPUT_DIR / "run_log.csv"
SHEET_NAME = "Products"
INDEX_CACHE = CACHE_DIR / "match_index.pkl"

SITES = {
    "kogan": {
        "index": "https://www.kogan.com/sitemap.xml",
        "is_product": lambda u: "/au/buy/" in u,
        "cache": CACHE_DIR / "kogan_urls.json",
    },
    "bigw": {
        "index": "https://www.bigw.com.au/sitemap.xml",
        "is_product": lambda u: "/product/" in u and "/p/" in u,
        "cache": CACHE_DIR / "bigw_urls.json",
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# --- scoring weights & thresholds (tune after pilot) ---
W_VARIANT_ATTR = 25  # per variant attribute (colour/size/volume) found in slug
W_MODEL_TOKEN = 8  # per model/descriptor token matched
W_FUZZY = 0.4  # multiplier on rapidfuzz token_sort_ratio (0-100) tiebreaker
MATCH_THRESHOLD = 55  # >= this AND all variant attrs present -> MATCH
PARTIAL_THRESHOLD = 30  # >= this but attrs missing -> PARTIAL

WRITE_PARTIAL_URL = False  # PARTIAL keeps best-guess URL in note/run_log only

SAVE_EVERY = 200  # save xlsx every N rows

# --- verification (Stage 4) ---
VERIFY_DELAY_SEC = 1.5  # polite delay between page fetches
VERIFY_TIMEOUT_SEC = 30
VERIFY_FUZZY_MIN = 70  # brand+name fuzzy score below this -> downgrade to PARTIAL

STOPWORDS = {
    "the", "with", "for", "and", "a", "an", "of", "to", "in",
    "x", "pcs", "pack", "set", "new",
}

KNOWN_BRANDS = {
    "devanti", "giselle", "artiss", "gardeon", "everfit", "weisshorn",
    "alfordson", "keezi", "kingston", "randy", "fortia", "vidalido",
}  # extend as needed

# variant attribute vocab
COLOURS = {
    "black", "white", "grey", "gray", "silver", "gold", "blue", "red",
    "green", "pink", "beige", "brown", "natural", "oak", "walnut",
    "charcoal", "cream", "navy", "teal",
}
SIZES = {
    "single", "king", "queen", "double", "super", "cot", "long",
    "small", "medium", "large", "xl", "kingsingle",
}
