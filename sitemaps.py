"""Stage 1+2: download retailer sitemaps → parse → filter → cache → match index."""

from __future__ import annotations

import gzip
import json
import pickle
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

from lxml import etree

from config import CACHE_DIR, HEADERS, INDEX_CACHE, SITES, STOPWORDS


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _decompress(url: str, content: bytes) -> bytes:
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        return gzip.decompress(content)
    return content


def _is_blocked(status: int, content: bytes) -> bool:
    if status == 403:
        return True
    head = content[:800].lower()
    return b"datadome" in head or b'id="cmsg"' in head or b"please enable" in head


def _fetch_http(url: str, retries: int = 3) -> bytes:
    """GET with curl_cffi Chrome impersonation (falls back to requests)."""
    last_err: Exception | None = None

    def _once(get_fn) -> bytes:
        resp = get_fn()
        body = resp.content
        if _is_blocked(resp.status_code, body):
            raise RuntimeError(f"Bot block ({resp.status_code}) for {url}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} for {url}")
        return _decompress(url, body)

    for attempt in range(retries):
        try:
            try:
                from curl_cffi import requests as creq

                return _once(
                    lambda: creq.get(
                        url, headers=HEADERS, impersonate="chrome", timeout=90
                    )
                )
            except ImportError:
                import requests

                return _once(lambda: requests.get(url, headers=HEADERS, timeout=90))
        except Exception as exc:
            last_err = exc
            wait = 2 ** attempt
            print(f"  retry {attempt + 1}/{retries} for {url}: {exc} (sleep {wait}s)")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def _parse_xml(content: bytes) -> etree._Element:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    return etree.fromstring(content, parser=parser)


def _child_locs(root: etree._Element) -> list[str]:
    locs: list[str] = []
    for el in root.iter():
        if _local(el.tag) == "loc" and el.text:
            locs.append(el.text.strip())
    return locs


def _edge_binary() -> str | None:
    from pathlib import Path

    for p in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ):
        if p.exists():
            return str(p)
    return None


def _extract_xml_from_driver(driver) -> bytes | None:
    from selenium.webdriver.common.by import By

    try:
        el = driver.find_element(By.ID, "webkit-xml-viewer-source-xml")
        text = (el.get_attribute("innerHTML") or "").strip()
        if text.startswith("<sitemap") or text.startswith("<urlset"):
            text = '<?xml version="1.0" encoding="UTF-8"?>\n' + text
        if text.startswith("<?xml") or text.startswith("<"):
            return text.encode("utf-8", errors="replace")
    except Exception:
        pass

    # Some Edge builds stash raw XML in a <script type="text/xml"> or pre
    try:
        for el in driver.find_elements(By.TAG_NAME, "script"):
            t = (el.get_attribute("innerHTML") or "").strip()
            if "<sitemapindex" in t or "<urlset" in t:
                if not t.startswith("<?xml"):
                    # find the xml start
                    for marker in ("<?xml", "<sitemapindex", "<urlset"):
                        idx = t.find(marker)
                        if idx != -1:
                            t = t[idx:]
                            break
                return t.encode("utf-8", errors="replace")
    except Exception:
        pass

    src = driver.page_source or ""
    for tag in ("sitemapindex", "urlset"):
        start = src.find(f"<{tag}")
        end = src.find(f"</{tag}>")
        if start != -1 and end != -1:
            chunk = src[start : end + len(f"</{tag}>")]
            return (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                + chunk.encode("utf-8", errors="replace")
            )
    return None


def _looks_like_datadome(html: str) -> bool:
    h = (html or "").lower()
    return "datadome" in h or 'id="cmsg"' in h or "please enable" in h


def _browser_session_fetch(
    seed_url: str,
    child_urls: list[str],
    batch_size: int = 5,
    batch_delay: float = 2.0,
) -> dict[str, bytes]:
    """
    Open one headed Edge/Chrome window, load seed_url (clears DataDome),
    then fetch child_urls via in-page fetch() in the same session.

    Rate-limits politely and re-primes the session when DataDome starts
    returning 403s on child sitemaps.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions

    binary = _edge_binary()
    if not binary:
        raise RuntimeError(
            "Bot protection blocked HTTP and no Edge/Chrome binary was found."
        )

    use_edge = "edge" in binary.lower()
    if use_edge:
        opts = EdgeOptions()
        opts.add_argument("--window-size=1280,800")
        opts.binary_location = binary
        driver = webdriver.Edge(options=opts)
    else:
        opts = ChromeOptions()
        opts.add_argument("--window-size=1280,800")
        opts.binary_location = binary
        driver = webdriver.Chrome(options=opts)

    results: dict[str, bytes] = {}
    fetch_script = """
        const urls = arguments[0];
        const done = arguments[arguments.length - 1];
        (async () => {
            const out = [];
            for (const url of urls) {
                try {
                    const r = await fetch(url, {credentials: 'include'});
                    const text = await r.text();
                    out.push({url, status: r.status, text});
                } catch (e) {
                    out.push({url, status: 0, text: '', error: String(e)});
                }
                await new Promise(r => setTimeout(r, 400));
            }
            done(out);
        })();
    """

    def _prime() -> None:
        print(f"  [browser] priming on {seed_url}")
        driver.get(seed_url)
        # Wait out DataDome interstitial if present
        for wait_i in range(12):
            time.sleep(3)
            src = driver.page_source or ""
            xml0 = _extract_xml_from_driver(driver)
            if xml0 and (b"<sitemapindex" in xml0 or b"<urlset" in xml0):
                results[seed_url] = xml0
                print(f"  [browser] seed ok ({len(xml0)} bytes)")
                return
            if _looks_like_datadome(src):
                print(f"  [browser] DataDome challenge pending... ({wait_i + 1}/12)")
                continue
            print(f"  [browser] waiting for XML... page_len={len(src)} ({wait_i + 1}/12)")
        # Last attempt dump
        src = driver.page_source or ""
        print(f"  [browser] FAIL title={driver.title!r} url={driver.current_url} len={len(src)}")
        print(f"  [browser] head={src[:300]!r}")
        raise RuntimeError(f"Browser loaded {seed_url} but could not extract XML")

    def _accept(item: dict) -> bool:
        status = item.get("status", 0)
        text = item.get("text") or ""
        raw = text.encode("utf-8", errors="replace")
        if status == 200 and (
            b"<urlset" in raw[:800]
            or b"<sitemapindex" in raw[:800]
            or b"<?xml" in raw[:120]
        ):
            results[item["url"]] = _decompress(item["url"], raw)
            return True
        return False

    try:
        driver.set_page_load_timeout(90)
        driver.set_script_timeout(600)
        _prime()

        remaining = [u for u in child_urls if u != seed_url and u not in results]
        failed: list[str] = []
        total_batches = max(1, (len(remaining) + batch_size - 1) // batch_size)

        for i in range(0, len(remaining), batch_size):
            batch = remaining[i : i + batch_size]
            batch_results = driver.execute_async_script(fetch_script, batch)
            ok = sum(1 for item in batch_results if _accept(item))
            blocked = [item["url"] for item in batch_results if item.get("status") == 403]
            other_fail = [
                item["url"]
                for item in batch_results
                if item["url"] not in results and item.get("status") != 403
            ]
            failed.extend(other_fail)

            print(
                f"  [browser] batch {i // batch_size + 1}/{total_batches}: "
                f"{ok}/{len(batch)} ok | blocked={len(blocked)} | total {len(results)}"
            )

            # Re-prime + retry when DataDome rate-limits the batch
            if len(blocked) >= max(1, len(batch) // 2):
                print(
                    f"  [browser] rate-limited ({len(blocked)} 403s); "
                    f"cooling 45s and re-priming"
                )
                time.sleep(45)
                _prime()
                time.sleep(2)
                retry = driver.execute_async_script(fetch_script, blocked)
                rok = sum(1 for item in retry if _accept(item))
                still = [item["url"] for item in retry if item["url"] not in results]
                failed.extend(still)
                print(f"  [browser] retry: {rok}/{len(blocked)} recovered")
                if rok == 0:
                    print("  [browser] still blocked — cooling 90s")
                    time.sleep(90)
                    _prime()

            time.sleep(batch_delay)

        # Final pass over failures (sequential, slow)
        failed = [u for u in dict.fromkeys(failed) if u not in results]
        if failed:
            print(f"  [browser] final pass over {len(failed)} failed URLs")
            time.sleep(30)
            _prime()
            for j in range(0, len(failed), batch_size):
                batch = failed[j : j + batch_size]
                batch_results = driver.execute_async_script(fetch_script, batch)
                ok = sum(1 for item in batch_results if _accept(item))
                print(
                    f"  [browser] final {j // batch_size + 1}: "
                    f"{ok}/{len(batch)} | total {len(results)}"
                )
                if ok == 0:
                    time.sleep(60)
                    _prime()
                else:
                    time.sleep(batch_delay)
    finally:
        driver.quit()
    return results


def _kogan_product_sitemap(url: str) -> bool:
    u = url.lower()
    return "/au/" in u and (
        "sitemap-products" in u
        or "sitemap-marketplace-products" in u
        or "sitemap-marketplace-minimum-sold-products" in u
    )


def _ingest_sitemap_bytes(
    content: bytes,
    is_product: Callable[[str], bool],
    product_urls: set[str],
) -> tuple[str, list[str], int]:
    """Parse one sitemap body. Returns (root_tag, child_locs_or_empty, urls_seen)."""
    root = _parse_xml(content)
    tag = _local(root.tag).lower()
    locs = _child_locs(root)
    if tag == "sitemapindex":
        return tag, locs, 0
    seen = 0
    for loc in locs:
        seen += 1
        if is_product(loc):
            product_urls.add(loc)
    return tag, [], seen


def crawl_site(name: str, refresh: bool = False) -> dict:
    """
    Stage 1 — download sitemap tree for a site, keep product URLs, write JSON cache.
    """
    if name not in SITES:
        raise ValueError(f"Unknown site: {name!r}. Choose from {list(SITES)}")

    site = SITES[name]
    cache_path = site["cache"]
    is_product: Callable[[str], bool] = site["is_product"]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and not refresh:
        print(f"[{name}] Using cached URLs from {cache_path}")
        with open(cache_path, encoding="utf-8") as f:
            payload = json.load(f)
        print(
            f"[{name}] Cached: {payload.get('sitemaps_parsed', '?')} sitemaps, "
            f"{len(payload.get('product_urls', []))} product URLs "
            f"(fetched_at={payload.get('fetched_at')})"
        )
        return payload

    print(f"[{name}] Crawling sitemap index: {site['index']}")
    product_urls: set[str] = set()
    total_urls_seen = 0
    sitemaps_parsed = 0
    t0 = time.time()
    last_log = t0

    # --- HTTP BFS (works for BigW; Kogan usually needs browser) ---
    http_ok = True
    queue: list[str] = [site["index"]]
    seen_sitemaps: set[str] = set()

    while queue and http_ok:
        sm_url = queue.pop(0)
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)
        try:
            content = _fetch_http(sm_url)
            tag, nested, seen = _ingest_sitemap_bytes(content, is_product, product_urls)
            sitemaps_parsed += 1
            total_urls_seen += seen
            if tag == "sitemapindex":
                for loc in nested:
                    if loc not in seen_sitemaps:
                        queue.append(loc)
        except Exception as exc:
            if "Bot block" in str(exc) or "403" in str(exc):
                print(f"[{name}] HTTP blocked ({exc}); switching to browser fallback")
                http_ok = False
                # put this URL back for browser path
                seen_sitemaps.discard(sm_url)
                queue.insert(0, sm_url)
                break
            print(f"  WARN: skipping {sm_url}: {exc}")

        now = time.time()
        if now - last_log >= 25 or sitemaps_parsed % 25 == 0:
            print(
                f"[{name}] {sitemaps_parsed} sitemaps parsed | "
                f"queue={len(queue)} | product_urls={len(product_urls)} | "
                f"elapsed={now - t0:.0f}s"
            )
            last_log = now

    # --- Browser fallback (Kogan / DataDome) ---
    if not http_ok:
        seed = site["index"]
        # Load index in browser, then all relevant children in one session.
        primed = _browser_session_fetch(seed, [], batch_size=1)
        index_content = primed.get(seed)
        if not index_content:
            raise RuntimeError(f"[{name}] Browser could not load sitemap index")

        tag, children, seen = _ingest_sitemap_bytes(
            index_content, is_product, product_urls
        )
        sitemaps_parsed = 1
        total_urls_seen += seen
        seen_sitemaps = {seed}

        if tag == "sitemapindex":
            if name == "kogan":
                to_fetch = [u for u in children if _kogan_product_sitemap(u)]
            else:
                to_fetch = list(children)
            print(
                f"[{name}] Index has {len(children)} children; "
                f"fetching {len(to_fetch)} product sitemaps via browser"
            )

            pending = list(to_fetch)

            def _save_checkpoint() -> None:
                partial = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "sitemaps_parsed": sitemaps_parsed,
                    "total_urls_seen": total_urls_seen,
                    "product_urls": sorted(product_urls),
                    "partial": True,
                }
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(partial, f)
                print(
                    f"  [checkpoint] {len(product_urls)} product URLs -> {cache_path}"
                )

            while pending:
                wave = [u for u in pending if u not in seen_sitemaps]
                pending = []
                if not wave:
                    break
                # Fetch in chunks so we can checkpoint between browser sessions
                chunk_size = 80
                for chunk_start in range(0, len(wave), chunk_size):
                    chunk = wave[chunk_start : chunk_start + chunk_size]
                    batch = _browser_session_fetch(
                        seed, chunk, batch_size=5, batch_delay=2.0
                    )
                    for sm_url, content in batch.items():
                        if sm_url == seed or sm_url in seen_sitemaps:
                            continue
                        seen_sitemaps.add(sm_url)
                        try:
                            ctag, nested, cseen = _ingest_sitemap_bytes(
                                content, is_product, product_urls
                            )
                        except Exception as exc:
                            print(f"  WARN: parse fail {sm_url}: {exc}")
                            continue
                        sitemaps_parsed += 1
                        total_urls_seen += cseen
                        if ctag == "sitemapindex":
                            for loc in nested:
                                if name == "kogan" and not _kogan_product_sitemap(loc):
                                    continue
                                if loc not in seen_sitemaps:
                                    pending.append(loc)

                    _save_checkpoint()
                    now = time.time()
                    print(
                        f"[{name}] {sitemaps_parsed} sitemaps parsed | "
                        f"product_urls={len(product_urls)} | "
                        f"elapsed={now - t0:.0f}s"
                    )
                    last_log = now
                    # brief pause between browser sessions
                    time.sleep(10)

    sorted_urls = sorted(product_urls)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sitemaps_parsed": sitemaps_parsed,
        "total_urls_seen": total_urls_seen,
        "product_urls": sorted_urls,
    }

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    print(
        f"[{name}] DONE: {sitemaps_parsed} sitemaps, "
        f"{total_urls_seen} URLs seen, {len(sorted_urls)} product URLs -> {cache_path}"
    )
    return payload


def _slug_from_url(url: str, site: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]

    if site == "kogan":
        try:
            idx = parts.index("buy")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass
        return parts[-1] if parts else ""

    if site == "bigw":
        try:
            idx = parts.index("product")
            if idx + 1 < len(parts):
                return parts[idx + 1]
        except ValueError:
            pass
        return parts[-1] if parts else ""

    return parts[-1] if parts else ""


def _tokenize_slug(slug: str) -> set[str]:
    raw = slug.lower().replace("_", "-").replace("/", "-")
    tokens: set[str] = set()
    for t in raw.split("-"):
        t = t.strip()
        if not t or t.isdigit() or t in STOPWORDS:
            continue
        tokens.add(t)
    return tokens


def build_index(site: str) -> tuple[dict, dict]:
    cache_path = SITES[site]["cache"]
    if not cache_path.exists():
        raise FileNotFoundError(
            f"No URL cache for {site} at {cache_path}. Run crawl_site('{site}') first."
        )

    with open(cache_path, encoding="utf-8") as f:
        payload = json.load(f)

    urls = payload["product_urls"]
    records: dict[str, dict] = {}
    inverted: dict[str, list[str]] = defaultdict(list)

    for url in urls:
        slug = _slug_from_url(url, site)
        tokens = _tokenize_slug(slug)
        records[url] = {"tokens": tokens, "slug": slug}
        for tok in tokens:
            inverted[tok].append(url)

    inverted_index = dict(inverted)
    print(
        f"[{site}] Index built: {len(records)} URLs, "
        f"{len(inverted_index)} unique tokens"
    )
    return records, inverted_index


def _load_index_cache() -> dict:
    if INDEX_CACHE.exists():
        with open(INDEX_CACHE, "rb") as f:
            return pickle.load(f)
    return {}


def _save_index_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_CACHE, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_or_build(site: str, refresh: bool = False) -> tuple[dict, dict]:
    crawl_site(site, refresh=refresh)

    url_cache = SITES[site]["cache"]
    url_mtime = url_cache.stat().st_mtime

    index_data = _load_index_cache()
    entry = index_data.get(site)
    if (
        not refresh
        and entry is not None
        and entry.get("url_mtime") == url_mtime
        and "records" in entry
        and "inverted_index" in entry
    ):
        print(f"[{site}] Using cached match index from {INDEX_CACHE}")
        records = entry["records"]
        inverted = entry["inverted_index"]
        print(f"[{site}] {len(records)} URLs, {len(inverted)} tokens")
        return records, inverted

    records, inverted = build_index(site)
    index_data[site] = {
        "url_mtime": url_mtime,
        "records": records,
        "inverted_index": inverted,
    }
    _save_index_cache(index_data)
    print(f"[{site}] Match index saved -> {INDEX_CACHE}")
    return records, inverted


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crawl retailer sitemaps")
    parser.add_argument("--site", choices=list(SITES), default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--index", action="store_true", help="Also build match index")
    args = parser.parse_args()

    sites = [args.site] if args.site else list(SITES)
    for s in sites:
        crawl_site(s, refresh=args.refresh)
        if args.index:
            load_or_build(s, refresh=False)
