"""Ingest custom retailer sitemaps into cache/custom/ and build match indexes."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from lxml import etree

from config import CACHE_DIR, HEADERS, ROOT, STOPWORDS

CUSTOM_DIR = CACHE_DIR / "custom"
CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY = CUSTOM_DIR / "registry.json"


def _load_registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    return {"sources": {}}


def _save_registry(data: dict) -> None:
    REGISTRY.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_sources() -> list[dict]:
    reg = _load_registry()
    # Built-in sources
    out = [
        {
            "id": "bigw",
            "name": "BigW",
            "index_url": "https://www.bigw.com.au/sitemap.xml",
            "builtin": True,
            "product_urls": _count_urls(CACHE_DIR / "bigw_urls.json"),
        },
        {
            "id": "kogan",
            "name": "Kogan",
            "index_url": "https://www.kogan.com/sitemap.xml",
            "builtin": True,
            "product_urls": _count_urls(CACHE_DIR / "kogan_urls.json"),
        },
    ]
    for sid, meta in reg.get("sources", {}).items():
        out.append({**meta, "id": sid, "builtin": False})
    return out


def _count_urls(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return len(json.loads(path.read_text(encoding="utf-8")).get("product_urls", []))
    except Exception:
        return 0


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:40] or f"source-{int(time.time())}"


def _fetch(url: str) -> bytes:
    try:
        from curl_cffi import requests as creq

        resp = creq.get(url, headers=HEADERS, impersonate="chrome", timeout=90)
    except Exception:
        import requests

        resp = requests.get(url, headers=HEADERS, timeout=90)

    content = resp.content
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} for {url}")
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        import gzip

        content = gzip.decompress(content)
    return content


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(content: bytes):
    return etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))


def _locs(root) -> list[str]:
    return [
        el.text.strip()
        for el in root.iter()
        if isinstance(el.tag, str) and _local(el.tag) == "loc" and el.text
    ]


def _default_is_product(url: str) -> bool:
    u = url.lower()
    return any(
        p in u
        for p in ("/product/", "/products/", "/au/buy/", "/buy/", "/p/", "/dp/")
    )


def crawl_custom_sitemap(
    index_url: str,
    name: str,
    product_pattern: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> dict:
    """
    Recursively crawl a sitemap index and store product URLs.
    product_pattern: optional regex; if omitted uses common product URL heuristics.
    """
    source_id = _slugify(name)
    if progress_cb:
        progress_cb(f"Starting crawl of {index_url}", 1)

    if product_pattern:
        cre = re.compile(product_pattern, re.I)

        def is_product(u: str) -> bool:
            return bool(cre.search(u))
    else:
        is_product = _default_is_product

    queue = [index_url]
    seen_sms: set[str] = set()
    product_urls: set[str] = set()
    total_seen = 0
    parsed = 0
    t0 = time.time()

    while queue:
        sm = queue.pop(0)
        if sm in seen_sms:
            continue
        seen_sms.add(sm)
        try:
            content = _fetch(sm)
            root = _parse(content)
        except Exception as exc:
            if progress_cb:
                progress_cb(f"Skip {sm}: {exc}", min(90, 5 + parsed))
            continue

        parsed += 1
        tag = _local(root.tag).lower()
        locs = _locs(root)
        if tag == "sitemapindex":
            for loc in locs:
                if loc not in seen_sms:
                    queue.append(loc)
        else:
            for loc in locs:
                total_seen += 1
                if is_product(loc):
                    product_urls.add(loc)

        if progress_cb and parsed % 5 == 0:
            progress_cb(
                f"Parsed {parsed} sitemaps · {len(product_urls)} product URLs",
                min(95, 5 + parsed * 0.5),
            )

    sorted_urls = sorted(product_urls)
    cache_path = CUSTOM_DIR / f"{source_id}_urls.json"
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sitemaps_parsed": parsed,
        "total_urls_seen": total_seen,
        "product_urls": sorted_urls,
        "index_url": index_url,
        "name": name,
        "product_pattern": product_pattern,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    # Build token index pickle sibling
    records, inverted = _build_index(sorted_urls)
    index_path = CUSTOM_DIR / f"{source_id}_index.pkl"
    import pickle

    with open(index_path, "wb") as f:
        pickle.dump({"records": records, "inverted_index": inverted}, f)

    reg = _load_registry()
    reg.setdefault("sources", {})[source_id] = {
        "name": name,
        "index_url": index_url,
        "product_pattern": product_pattern,
        "cache": str(cache_path.relative_to(ROOT)),
        "index": str(index_path.relative_to(ROOT)),
        "product_urls": len(sorted_urls),
        "fetched_at": payload["fetched_at"],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    _save_registry(reg)

    if progress_cb:
        progress_cb(f"Done — {len(sorted_urls)} product URLs stored as '{source_id}'", 100)

    return reg["sources"][source_id] | {"id": source_id}


def _tokenize(slug: str) -> set[str]:
    raw = slug.lower().replace("_", "-").replace("/", "-")
    tokens = set()
    for t in raw.split("-"):
        t = t.strip()
        if not t or t.isdigit() or t in STOPWORDS:
            continue
        tokens.add(t)
    return tokens


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if "product" in parts:
        i = parts.index("product")
        if i + 1 < len(parts):
            return parts[i + 1]
    if "buy" in parts:
        i = parts.index("buy")
        if i + 1 < len(parts):
            return parts[i + 1]
    if "products" in parts:
        i = parts.index("products")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else ""


def _build_index(urls: list[str]) -> tuple[dict, dict]:
    records = {}
    inverted: dict[str, list[str]] = defaultdict(list)
    for url in urls:
        slug = _slug_from_url(url)
        tokens = _tokenize(slug)
        records[url] = {"tokens": tokens, "slug": slug}
        for tok in tokens:
            inverted[tok].append(url)
    return records, dict(inverted)


def load_site_index(site_id: str) -> tuple[dict, dict]:
    """Load records + inverted index for builtin or custom site."""
    if site_id in ("bigw", "kogan"):
        import sitemaps

        return sitemaps.load_or_build(site_id, refresh=False)

    reg = _load_registry()
    meta = reg.get("sources", {}).get(site_id)
    if not meta:
        raise FileNotFoundError(f"Unknown sitemap source: {site_id}")
    index_path = ROOT / meta["index"]
    import pickle

    with open(index_path, "rb") as f:
        data = pickle.load(f)
    return data["records"], data["inverted_index"]
