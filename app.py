import sys
import re
import copy
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, redirect, render_template, request, url_for
from pymongo import MongoClient
from bson import ObjectId

# Reuse the modular deal-finder logic from project/
PROJECT_DIR = Path(__file__).resolve().parent / "project"
if str(PROJECT_DIR) not in sys.path:
    sys.path.append(str(PROJECT_DIR))

from ai_parser import parse_user_input
from config import get_config
from price_engine import calculate_price_metrics, parse_quantity_details
from scraper import scrape_product_data
from search import search_product_urls


app = Flask(__name__)

DB_NAME = "deal_finder"
COLLECTION_NAME = "user_texts"
TRACKED_COLLECTION_NAME = "tracked_products"
CACHE_TTL_SECONDS = 0
SEARCH_MAX_RESULTS = 40
VARIANT_MAX_EXTRA = 40
SCRAPE_WORKERS = 12
HOURLY_INTERVAL_SECONDS = 3600
_BEST_OFFER_CACHE: Dict[str, Dict] = {}
_SCHEDULER_STARTED = False


def _collection():
    cfg = get_config()
    client = MongoClient(
        cfg["mongo_uri"],
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=3000,
    )
    return client[DB_NAME][COLLECTION_NAME]


def _tracked_collection():
    cfg = get_config()
    client = MongoClient(
        cfg["mongo_uri"],
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=3000,
    )
    return client[DB_NAME][TRACKED_COLLECTION_NAME]


def _tracked_for_view() -> List[dict]:
    rows = list(
        _tracked_collection()
        .find({"notify_enabled": True})
        .sort("updated_at", -1)
        .limit(20)
    )
    for r in rows:
        r["_id"] = str(r.get("_id"))
        if r.get("source_text_id") is not None:
            r["source_text_id"] = str(r.get("source_text_id"))
    return rows


def _send_baileys_best_deal_notification(item: Dict, best: Optional[Dict]) -> None:
    if not best:
        return
    cfg = get_config()
    webhook = cfg.get("baileys_webhook_url")
    to = cfg.get("baileys_to")
    if not webhook or not to:
        return
    msg = (
        f"Best deal update\n"
        f"Query: {item.get('normalized_query')}\n"
        f"Effective: Rs {best.get('effective_price_for_requested_qty')}\n"
        f"Final: Rs {best.get('final_price')}\n"
        f"Title: {best.get('title')}\n"
        f"URL: {best.get('url')}"
    )
    try:
        requests.post(
            webhook,
            json={"to": to, "message": msg},
            timeout=12,
        )
    except Exception:
        pass


def _to_base_quantity(quantity_text: Optional[str]) -> Optional[float]:
    if not quantity_text:
        return None
    text = quantity_text.lower().replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    if unit in {"kg", "l"}:
        return value * 1000
    return value


def _is_quantity_match(requested_base: Optional[float], offer_total_base: Optional[float], pack_count: Optional[int]) -> bool:
    """
    Accept only offers that match requested pack size (e.g., 150g).
    For combos, compare per-pack quantity: total / pack_count.
    """
    if requested_base is None:
        return True
    if offer_total_base is None:
        return False

    per_pack = offer_total_base
    if pack_count and pack_count > 1:
        per_pack = offer_total_base / float(pack_count)

    # 5% tolerance for small parsing noise.
    tolerance = requested_base * 0.05
    abs_tol = max(1.0, tolerance)
    if abs(per_pack - requested_base) <= abs_tol:
        return True

    # Fallback: allow exact multiples even when pack_count parsing is missing.
    # Example: requested 150g, offer total 600g -> 4 units.
    if offer_total_base >= requested_base:
        ratio = float(offer_total_base) / float(requested_base)
        nearest = round(ratio)
        if nearest >= 1 and abs(ratio - nearest) <= 0.08:
            return True

    return False


def _extract_offer_total_qty_from_title(title: str, fallback_qty: Optional[str]) -> Optional[float]:
    raw = (title or "").lower()
    t = raw.replace(" ", "")
    has_combo_hint = any(k in raw for k in ["combo", "pack of", " x ", "*"])

    # Examples: 150g*4, 150gx4, 4x150g
    if has_combo_hint:
        m1 = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)[x*](\d+)", t)
        if m1:
            base = _to_base_quantity(f"{m1.group(1)}{m1.group(2)}")
            if base is not None:
                return base * float(m1.group(3))

        m2 = re.search(r"(\d+)[x*](\d+(?:\.\d+)?)(g|gm|kg|ml|l)", t)
        if m2:
            base = _to_base_quantity(f"{m2.group(2)}{m2.group(3)}")
            if base is not None:
                return base * float(m2.group(1))

    # Example: "600g, pack of 4" -> use 600g as total quantity in listing.
    m_total = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)", t)
    if has_combo_hint and m_total and re.search(r"packof\d+", t):
        total = _to_base_quantity(f"{m_total.group(1)}{m_total.group(2)}")
        if total is not None:
            return total

    return _to_base_quantity(fallback_qty)


def _extract_urls(text: str) -> List[str]:
    return re.findall(r"https?://[^\s]+", text or "")


def _normalized_query_content(product: Optional[str], quantity: Optional[str]) -> str:
    p = (product or "").strip()
    q = (quantity or "").strip()
    if p and q:
        return f"{p} - {q}"
    return p or q


def _tracked_key(product: Optional[str], quantity: Optional[str]) -> str:
    return _normalized_query_content(product, quantity).lower()


def _cache_key(
    product: str,
    quantity: Optional[str],
    target_price: Optional[float],
    preferred_urls: Optional[List[str]],
) -> str:
    p = (product or "").strip().lower()
    q = (quantity or "").strip().lower()
    t = "" if target_price is None else str(float(target_price))
    u = "|".join(sorted(set(preferred_urls or [])))
    return f"{p}||{q}||{t}||{u}"


def _seed_known_variant_urls(product: str, quantity: Optional[str]) -> List[str]:
    """
    Deterministic fallback seeds for known hard-to-discover variant URLs.
    """
    p = (product or "").lower()
    q = (quantity or "").lower()
    seeds: List[str] = []

    if "colgate" in p and "maxfresh" in p and ("peppermint" in p or "ice" in p) and "150" in q:
        seeds.append(
            "https://www.flipkart.com/colgate-maxfresh-blue-gel-peppermint-ice-toothpaste/p/itmfehguhjbuhnjm"
        )
    return seeds


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _is_relevant_title(title: str, product: str) -> bool:
    """
    Keep only listings that match key product words from user intent.
    """
    title_tokens = set(_tokenize(title))
    product_tokens = [t for t in _tokenize(product) if len(t) > 2]
    if not product_tokens:
        return True
    hits = [t for t in product_tokens if t in title_tokens]
    hit_count = len(hits)

    # Require key intent tokens to avoid unrelated bundles.
    must_have_tokens = []
    for core in ("colgate", "maxfresh"):
        if core in product_tokens:
            must_have_tokens.append(core)
    if must_have_tokens and not all(t in title_tokens for t in must_have_tokens):
        return False

    # Keep recall broad, but still require at least two matching tokens.
    required = min(2, len(product_tokens))
    if hit_count < required:
        return False

    # Reject clearly mixed-product bundles that are not toothpaste-first.
    low_title = (title or "").lower()
    blocked_terms = {
        "face mask",
        "shampoo",
        "soap",
        "conditioner",
        "serum",
        "cream",
        "lotion",
    }
    if any(term in low_title for term in blocked_terms):
        # Allow only if title explicitly stays focused on toothpaste sku wording.
        if "toothpaste" not in low_title:
            return False
        # Even with toothpaste present, mixed cosmetics are noisy for this app.
        return False

    return True


def _expand_flipkart_urls(seed_urls: List[str], product: str, max_extra: int = VARIANT_MAX_EXTRA) -> List[str]:
    """
    Second-pass expansion:
    From each Flipkart PDP, collect more /p/ links that match product tokens.
    This helps discover pack variants (pack of 2/4/etc.) that search missed.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
    }
    tokens = [t for t in _tokenize(product) if len(t) > 2]
    out: List[str] = []
    seen = set(seed_urls)

    for url in seed_urls:
        if "flipkart.com" not in url:
            continue
        try:
            html = requests.get(url, headers=headers, timeout=20).text
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a[href*='/p/']"):
                href = a.get("href", "")
                if not href:
                    continue
                full = href if href.startswith("http") else f"https://www.flipkart.com{href}"
                full = full.split("?")[0]
                low = full.lower()
                # Keep links that look relevant to query tokens.
                hit_count = sum(1 for t in tokens if t in low)
                if hit_count < min(2, len(tokens)):
                    continue
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                if len(out) >= max_extra:
                    return out
        except Exception:
            continue
    return out


def _build_best_offer(
    product: str,
    quantity: Optional[str],
    target_price: Optional[float],
    preferred_urls: Optional[List[str]] = None,
) -> Dict:
    key = _cache_key(product, quantity, target_price, preferred_urls)
    now = time.time()
    cached = _BEST_OFFER_CACHE.get(key)
    if cached and (now - cached.get("ts", 0)) <= CACHE_TTL_SECONDS:
        result = copy.deepcopy(cached["data"])
        if isinstance(result, dict):
            debug = result.get("debug") or {}
            debug["cache_hit"] = True
            debug["cache_ttl_seconds"] = CACHE_TTL_SECONDS
            result["debug"] = debug
        return result

    query = f"{product} {quantity or ''}".strip()
    requested_details = parse_quantity_details(quantity) if quantity else None
    requested_qty_base = requested_details["quantity_per_unit"] if requested_details else None
    urls = search_product_urls(query, max_results=SEARCH_MAX_RESULTS)
    urls = list(dict.fromkeys(_seed_known_variant_urls(product, quantity) + urls))
    if preferred_urls:
        # Prefer user-provided URLs first and keep order unique.
        urls = list(dict.fromkeys(preferred_urls + urls))
    # Expand with related Flipkart variant links.
    variant_urls = _expand_flipkart_urls(urls, product=product, max_extra=VARIANT_MAX_EXTRA)
    if variant_urls:
        urls = list(dict.fromkeys(urls + variant_urls))
    if not urls:
        return {"error": "No product links found on Amazon/Flipkart.", "debug": {"query": query, "urls": []}}

    candidates = []
    scrape_attempts = []
    scraped_by_url: Dict[str, Optional[Dict]] = {}
    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as executor:
        future_map = {executor.submit(scrape_product_data, url): url for url in urls}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                scraped_by_url[url] = future.result()
            except Exception:
                scraped_by_url[url] = None

    for url in urls:
        scraped = scraped_by_url.get(url)
        scrape_attempts.append({"url": url, "status": "ok" if scraped else "failed"})
        if not scraped:
            continue
        if not _is_relevant_title(scraped.get("title", ""), product):
            scrape_attempts[-1]["status"] = "filtered_not_relevant"
            continue

        metrics = calculate_price_metrics(scraped, requested_quantity=quantity)
        qd = metrics.get("quantity_details") or {}
        quantity_per_unit = qd.get("quantity_per_unit")
        total_quantity = metrics.get("total_quantity")
        pack_count = qd.get("units") or scraped.get("pack_count")

        # Quantity compatibility check:
        # Allow exact per-pack matches (150g), and larger packs/combo totals
        # that contain the requested size (e.g., 600g as 4x150g).
        if requested_qty_base is not None and not _is_quantity_match(
            requested_base=requested_qty_base,
            offer_total_base=total_quantity,
            pack_count=pack_count,
        ):
            scrape_attempts[-1]["status"] = "filtered_quantity_mismatch"
            continue

        candidates.append(
            {
                "title": scraped.get("title"),
                "url": url,
                "base_price": metrics.get("base_price"),
                "final_price": metrics.get("final_price"),
                "price_per_unit": metrics.get("price_per_unit"),
                "total_quantity": total_quantity,
                "unit": metrics.get("unit"),
                "units": qd.get("units"),
                "quantity_per_unit": quantity_per_unit,
                "pack_count": pack_count,
                "effective_price_for_requested_qty": metrics.get("effective_price"),
                "offers": scraped.get("offers", []),
                "quantity": scraped.get("quantity"),
            }
        )

    if not candidates:
        return {
            "error": "Search succeeded, but scraping failed for all results.",
            "debug": {"query": query, "urls": urls, "scrape_attempts": scrape_attempts},
        }

    # Best offer must be based on normalized price_per_unit.
    comparable = [c for c in candidates if c.get("price_per_unit") is not None]
    pick_pool = comparable if comparable else candidates

    # Optional target filter uses effective price for requested qty.
    within_target = []
    if target_price is not None:
        for c in pick_pool:
            effective = c.get("effective_price_for_requested_qty")
            if effective is not None and float(effective) <= float(target_price):
                within_target.append(c)

    pick_from = within_target if within_target else pick_pool
    best = min(
        pick_from,
        key=lambda x: (
            x.get("price_per_unit") if x.get("price_per_unit") is not None else float("inf"),
            x.get("effective_price_for_requested_qty")
            if x.get("effective_price_for_requested_qty") is not None
            else float("inf"),
            x.get("url", ""),
        ),
    )
    best["is_deal"] = (
        target_price is not None
        and best.get("effective_price_for_requested_qty") is not None
        and float(best.get("effective_price_for_requested_qty")) <= float(target_price)
    )

    best["target_price"] = target_price
    candidates.sort(
        key=lambda x: (
            x.get("effective_price_for_requested_qty")
            if x.get("effective_price_for_requested_qty") is not None
            else (x.get("price_per_unit") if x.get("price_per_unit") is not None else float("inf")),
            x.get("price_per_unit") if x.get("price_per_unit") is not None else float("inf"),
            x.get("url", ""),
        )
    )
    for c in candidates:
        c["best_offer"] = c["url"] == best["url"]
    top_offers = candidates[:3]
    result = {
        "best": best,
        "top_offers": top_offers,
        "products": candidates,
        "debug": {
            "query": query,
            "urls": urls,
            "scrape_attempts": scrape_attempts,
            "candidate_count": len(candidates),
            "cache_hit": False,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
        },
    }
    _BEST_OFFER_CACHE[key] = {"ts": now, "data": copy.deepcopy(result)}
    return result


def _hourly_notify_scan_once() -> None:
    tracked = list(_tracked_collection().find({"notify_enabled": True}))
    for item in tracked:
        try:
            bundle = _build_best_offer(
                product=item.get("product"),
                quantity=item.get("quantity"),
                target_price=item.get("target_price"),
                preferred_urls=item.get("preferred_urls") or [],
            )
        except Exception:
            continue

        now_dt = datetime.now(UTC)
        top3 = (bundle.get("top_offers") or [])[:3]
        best = bundle.get("best")
        prev_best_effective = item.get("best_effective_price")
        curr_best_effective = best.get("effective_price_for_requested_qty") if best else None
        history_item = {
            "checked_at": now_dt,
            "top3": top3,
            "best": best,
        }
        _tracked_collection().update_one(
            {"_id": item["_id"]},
            {
                "$set": {
                    "latest_top3": top3,
                    "latest_best": best,
                    "latest_scan_at": now_dt,
                    "latest_products_count": len(bundle.get("products") or []),
                    "best_effective_price": curr_best_effective,
                    "last_checked_at": now_dt,
                    "updated_at": now_dt,
                },
                "$push": {"price_history": history_item},
            },
        )
        if curr_best_effective is not None:
            if prev_best_effective is None or float(curr_best_effective) < float(prev_best_effective):
                _send_baileys_best_deal_notification(item, best)


def _start_scheduler_once() -> None:
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    _SCHEDULER_STARTED = True

    def _loop():
        while True:
            try:
                _hourly_notify_scan_once()
            except Exception:
                pass
            time.sleep(HOURLY_INTERVAL_SECONDS)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()


@app.route("/", methods=["GET"])
def home():
    _start_scheduler_once()
    texts: List[dict] = list(
        _collection().find({}, {"_id": 1, "content": 1, "created_at": 1}).sort("created_at", -1)
    )
    notify_text_ids = set(
        str(x.get("source_text_id"))
        for x in _tracked_collection().find({"notify_enabled": True}, {"source_text_id": 1})
        if x.get("source_text_id") is not None
    )
    for t in texts:
        t["notify_enabled"] = str(t.get("_id")) in notify_text_ids
    return render_template(
        "index.html",
        texts=texts,
        latest_result=None,
        tracked_rows=_tracked_for_view(),
        error=None,
    )


@app.route("/add", methods=["POST"])
def add_text():
    content = request.form.get("content", "").strip()
    texts: List[dict] = list(
        _collection().find({}, {"_id": 1, "content": 1, "created_at": 1}).sort("created_at", -1)
    )

    if not content:
        return render_template(
            "index.html",
            texts=texts,
            latest_result=None,
            tracked_rows=_tracked_for_view(),
            error="Please enter some text.",
        )

    cfg = get_config()
    groq_api_key = cfg.get("groq_api_key")
    if not groq_api_key:
        return render_template(
            "index.html",
            texts=texts,
            latest_result=None,
            tracked_rows=_tracked_for_view(),
            error="GROQ_API_KEY is not set. Please set it and retry.",
        )

    # Save raw input first for history.
    inserted = _collection().insert_one({"content": content, "created_at": datetime.now(UTC)})
    text_id = inserted.inserted_id

    try:
        parsed = parse_user_input(content, groq_api_key, model=cfg.get("groq_model", "llama-3.3-70b-versatile"))
        if not parsed.get("product"):
            raise ValueError("AI could not extract a valid product.")
    except Exception as exc:
        return render_template(
            "index.html",
            texts=texts,
            latest_result=None,
            tracked_rows=_tracked_for_view(),
            error=f"AI parsing failed: {exc}",
        )

    _collection().update_one(
        {"_id": text_id},
        {
            "$set": {
                "parsed_product": parsed.get("product"),
                "parsed_quantity": parsed.get("quantity"),
                "parsed_target_price": parsed.get("target_price"),
                "normalized_query": _normalized_query_content(parsed.get("product"), parsed.get("quantity")),
            }
        },
    )

    try:
        best_offer_bundle = _build_best_offer(
            product=parsed.get("product"),
            quantity=parsed.get("quantity"),
            target_price=parsed.get("target_price"),
            preferred_urls=_extract_urls(content),
        )
    except Exception as exc:
        best_offer_bundle = {"error": f"Search/Scrape error: {exc}"}

    latest_result = {
        "raw_text": content,
        "parsed": parsed,
        "best_offer": best_offer_bundle.get("best") if isinstance(best_offer_bundle, dict) else None,
        "top_offers": best_offer_bundle.get("top_offers", []) if isinstance(best_offer_bundle, dict) else [],
        "products": best_offer_bundle.get("products", []) if isinstance(best_offer_bundle, dict) else [],
        "debug": best_offer_bundle.get("debug") if isinstance(best_offer_bundle, dict) else None,
        "error": best_offer_bundle.get("error") if isinstance(best_offer_bundle, dict) else None,
    }

    texts = list(_collection().find({}, {"_id": 1, "content": 1, "created_at": 1}).sort("created_at", -1))
    notify_text_ids = set(
        str(x.get("source_text_id"))
        for x in _tracked_collection().find({"notify_enabled": True}, {"source_text_id": 1})
        if x.get("source_text_id") is not None
    )
    for t in texts:
        t["notify_enabled"] = str(t.get("_id")) in notify_text_ids
    return render_template(
        "index.html",
        texts=texts,
        latest_result=latest_result,
        tracked_rows=_tracked_for_view(),
        error=None,
    )


@app.route("/notify/<text_id>", methods=["POST"])
def toggle_notify(text_id: str):
    try:
        oid = ObjectId(text_id)
    except Exception:
        return redirect(url_for("home"))

    text_doc = _collection().find_one({"_id": oid})
    if not text_doc:
        return redirect(url_for("home"))

    product = text_doc.get("parsed_product")
    quantity = text_doc.get("parsed_quantity")
    target_price = text_doc.get("parsed_target_price")
    if not product:
        return redirect(url_for("home"))

    key = _tracked_key(product, quantity)
    now_dt = datetime.now(UTC)
    existing = _tracked_collection().find_one({"tracked_key": key})
    if existing and existing.get("notify_enabled"):
        _tracked_collection().update_one(
            {"_id": existing["_id"]},
            {"$set": {"notify_enabled": False, "updated_at": now_dt}},
        )
    else:
        _tracked_collection().update_one(
            {"tracked_key": key},
            {
                "$set": {
                    "product": product,
                    "quantity": quantity,
                    "target_price": target_price,
                    "normalized_query": _normalized_query_content(product, quantity),
                    "tracked_key": key,
                    "raw_text": text_doc.get("content"),
                    "source_text_id": oid,
                    "notify_enabled": True,
                    "updated_at": now_dt,
                },
                "$setOnInsert": {
                    "created_at": now_dt,
                    "price_history": [],
                    "latest_top3": [],
                },
            },
            upsert=True,
        )
        # Run one immediate scan for fresh baseline top-3.
        _hourly_notify_scan_once()
    return redirect(url_for("home"))


@app.route("/delete/<text_id>", methods=["POST"])
def delete_text(text_id: str):
    try:
        oid = ObjectId(text_id)
        _collection().delete_one({"_id": oid})
        _tracked_collection().update_many({"source_text_id": oid}, {"$set": {"notify_enabled": False}})
    except Exception:
        # Keep UI stable even if a delete fails.
        pass
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(debug=True)
