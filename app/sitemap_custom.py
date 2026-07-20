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
KOGAN_BRANDS_PATH = CACHE_DIR / "kogan_brands.json"


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
            "brand_count": len(load_kogan_brands()),
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


def _persist_builtin_urls(site_id: str, urls: list[str], source_label: str) -> dict:
    """Write builtin BigW/Kogan URL cache and rebuild match index."""
    import pickle

    import sitemaps
    from config import INDEX_CACHE, SITES

    cache_path = SITES[site_id]["cache"]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sitemaps_parsed": 0,
        "total_urls_seen": len(urls),
        "product_urls": urls,
        "index_url": SITES[site_id]["index"],
        "name": site_id,
        "source": source_label,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    records, inverted = sitemaps.build_index(site_id)
    index_data = sitemaps._load_index_cache()
    index_data[site_id] = {
        "url_mtime": cache_path.stat().st_mtime,
        "records": records,
        "inverted_index": inverted,
    }
    sitemaps._save_index_cache(index_data)

    return {
        "id": site_id,
        "name": site_id.capitalize() if site_id != "bigw" else "BigW",
        "builtin": True,
        "product_urls": len(urls),
        "cache": str(cache_path.relative_to(ROOT)),
        "index": str(INDEX_CACHE.relative_to(ROOT)),
        "fetched_at": payload["fetched_at"],
        "source": source_label,
    }


def _persist_custom_urls(
    name: str,
    urls: list[str],
    index_url: str | None,
    source_label: str,
) -> dict:
    import pickle

    source_id = _slugify(name)
    cache_path = CUSTOM_DIR / f"{source_id}_urls.json"
    index_path = CUSTOM_DIR / f"{source_id}_index.pkl"
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sitemaps_parsed": 0,
        "total_urls_seen": len(urls),
        "product_urls": urls,
        "index_url": index_url or "manual-bulk-upload",
        "name": name,
        "source": source_label,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    records, inverted = _build_index(urls)
    with open(index_path, "wb") as f:
        pickle.dump({"records": records, "inverted_index": inverted}, f)

    reg = _load_registry()
    reg.setdefault("sources", {})[source_id] = {
        "name": name,
        "index_url": index_url or "manual-bulk-upload",
        "product_pattern": None,
        "cache": str(cache_path.relative_to(ROOT)),
        "index": str(index_path.relative_to(ROOT)),
        "product_urls": len(urls),
        "fetched_at": payload["fetched_at"],
        "source": source_label,
    }
    _save_registry(reg)
    return reg["sources"][source_id] | {"id": source_id, "builtin": False}


def load_kogan_brands() -> list[str]:
    if not KOGAN_BRANDS_PATH.exists():
        return []
    try:
        data = json.loads(KOGAN_BRANDS_PATH.read_text(encoding="utf-8"))
        return list(data.get("brands") or [])
    except Exception:
        return []


def save_kogan_brands(brands: list[str], *, merge: bool = True) -> dict:
    """Persist Kogan brand slugs extracted from bulk sitemap dumps."""
    KOGAN_BRANDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = set(load_kogan_brands()) if merge else set()
    merged = sorted(existing | {b.lower().strip() for b in brands if b and b.strip()})
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "brands": merged,
        "count": len(merged),
    }
    KOGAN_BRANDS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def ingest_bulk_urls(
    name: str,
    raw_text: str,
    *,
    merge: bool = True,
    product_pattern: str | None = None,
    index_url: str | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
) -> dict:
    """
    Clean a bulk URL dump and store it as a matchable catalogue.
    name: 'kogan' / 'bigw' updates the built-in caches; anything else is custom.
    """
    from app.url_bulk import extract_bulk_catalogue

    if progress_cb:
        progress_cb("Cleaning product URLs…", 5)

    target = name.strip().lower()
    # Map display-ish names
    if target in {"kogan.com", "kogan au"}:
        target = "kogan"
    if target in {"big w", "big-w", "bigw.com.au"}:
        target = "bigw"

    extracted = extract_bulk_catalogue(
        raw_text,
        target=target if target in ("kogan", "bigw") else name,
        product_pattern=product_pattern,
    )
    new_urls = extracted["urls"]
    brands = extracted.get("brands") or []
    filtered = extracted.get("filtered") or {}
    if progress_cb:
        msg = f"Found {len(new_urls):,} product URLs"
        if brands:
            msg += f", {len(brands):,} brands"
        if any(filtered.values()):
            msg += f" ({sum(filtered.values()):,} category/collection rows filtered)"
        progress_cb(msg, 25)

    if not new_urls and not brands:
        raise ValueError(
            "No product URLs or brands found after cleaning. "
            "Paste Kogan sitemap dumps (/au/buy/, /au/{brand}/, brand-category URLs), "
            "BigW /product/.../p/… links, or set a product URL regex."
        )

    existing: set[str] = set()
    if merge:
        if target in ("kogan", "bigw"):
            from config import SITES

            cache_path = SITES[target]["cache"]
            if cache_path.exists():
                existing = set(
                    json.loads(cache_path.read_text(encoding="utf-8")).get("product_urls", [])
                )
        else:
            source_id = _slugify(name)
            cache_path = CUSTOM_DIR / f"{source_id}_urls.json"
            if cache_path.exists():
                existing = set(
                    json.loads(cache_path.read_text(encoding="utf-8")).get("product_urls", [])
                )

    before = len(existing)
    combined = sorted(existing | set(new_urls))
    added = len(combined) - before

    if progress_cb:
        progress_cb(
            f"{'Merging' if merge else 'Replacing'}: {len(combined):,} total "
            f"(+{added:,} new)",
            55,
        )

    source_label = "manual-bulk"
    meta: dict
    if new_urls:
        if target in ("kogan", "bigw"):
            meta = _persist_builtin_urls(target, combined, source_label)
        else:
            meta = _persist_custom_urls(name, combined, index_url, source_label)
    else:
        meta = {
            "id": target if target in ("kogan", "bigw") else _slugify(name),
            "name": name,
            "product_urls": len(combined),
            "builtin": target in ("kogan", "bigw"),
        }

    brand_result: dict | None = None
    if brands and target == "kogan":
        brand_result = save_kogan_brands(brands, merge=merge)
        if progress_cb:
            progress_cb(
                f"Stored {brand_result['count']:,} Kogan brands (+{len(brands):,} from upload)",
                90,
            )

    if progress_cb:
        if new_urls:
            progress_cb(
                f"Stored {meta['product_urls']:,} URLs in catalogue '{meta['id']}'",
                100,
            )
        else:
            progress_cb(f"Stored {brand_result['count'] if brand_result else 0:,} brands (no new product URLs)", 100)

    return {
        **meta,
        "unique_from_upload": len(new_urls),
        "added": added if new_urls else 0,
        "merged": merge,
        "brands_from_upload": len(brands),
        "brands_total": brand_result["count"] if brand_result else len(load_kogan_brands()),
        "filtered": filtered,
        "samples": extracted["samples"],
        "brand_samples": extracted.get("brand_samples", []),
        "rejected_samples": extracted["rejected_samples"],
    }
