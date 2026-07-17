"""Rebuild Kogan product URL cache from Wayback Machine (DataDome fallback)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from curl_cffi import requests as creq
from lxml import etree

from config import CACHE_DIR, SITES


def _log(msg: str) -> None:
    print(msg, flush=True)


def fetch_wb(ts: str, original: str) -> bytes | None:
    url = f"https://web.archive.org/web/{ts}id_/{original}"
    try:
        r = creq.get(url, impersonate="chrome", timeout=60)
        if r.status_code != 200:
            _log(f"  HTTP {r.status_code} {original}")
            return None
        if b"<urlset" not in r.content[:1000] and b"<sitemapindex" not in r.content[:1000]:
            _log(f"  not xml {original} len={len(r.content)}")
            return None
        return r.content
    except Exception as exc:
        _log(f"  ERR {original}: {exc}")
        return None


def product_locs(content: bytes) -> list[str]:
    root = etree.fromstring(
        content, parser=etree.XMLParser(recover=True, huge_tree=True)
    )
    out = []
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.endswith("loc") and el.text:
            u = el.text.strip()
            if "/au/buy/" in u:
                out.append(u)
    return out


def main() -> None:
    _log("Building Kogan cache from Wayback...")
    product_urls: set[str] = set()
    parsed = 0

    targets: list[tuple[str, str]] = [
        (
            "20260615013523",
            "https://www.kogan.com/au/sitemap-marketplace-products.xml",
        ),
        (
            "20250113205006",
            "https://www.kogan.com/sitemap.xml",
        ),
    ]

    for p in range(1, 80):
        page = "" if p == 1 else f"?p={p}"
        targets.append(
            (
                "20220902175701",
                f"https://www.kogan.com/au/sitemap-products.xml{page}",
            )
        )

    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for ts, orig in targets:
        if orig in seen:
            continue
        seen.add(orig)
        uniq.append((ts, orig))

    for i, (ts, original) in enumerate(uniq, 1):
        _log(f"[{i}/{len(uniq)}] {original}")
        content = fetch_wb(ts, original)
        if not content:
            continue

        if b"<sitemapindex" in content[:500]:
            root = etree.fromstring(
                content, parser=etree.XMLParser(recover=True, huge_tree=True)
            )
            children = []
            for el in root.iter():
                if isinstance(el.tag, str) and el.tag.endswith("loc") and el.text:
                    u = el.text.strip()
                    if "/au/" in u and "product" in u.lower():
                        children.append(u)
            _log(f"  index -> {len(children)} AU product children (taking 120)")
            for child in children[:120]:
                c = fetch_wb(ts, child)
                if not c:
                    continue
                locs = product_locs(c)
                product_urls.update(locs)
                parsed += 1
                _log(f"    +{len(locs)} total={len(product_urls)}")
                time.sleep(0.25)
            continue

        locs = product_locs(content)
        product_urls.update(locs)
        parsed += 1
        _log(f"  +{len(locs)} total={len(product_urls)}")
        time.sleep(0.25)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sitemaps_parsed": parsed,
        "total_urls_seen": len(product_urls),
        "product_urls": sorted(product_urls),
        "source": "wayback",
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = SITES["kogan"]["cache"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    _log(f"DONE Wrote {len(product_urls)} URLs -> {path}")


if __name__ == "__main__":
    main()
