import re
from typing import Any, Dict, Optional

import requests
from bs4 import BeautifulSoup


SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


def _parse_price(price_text: str) -> Optional[float]:
    cleaned = re.sub(r"[^\d.]", "", price_text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_quantity(text: str) -> Optional[str]:
    pattern = re.compile(r"(\d+\s?(?:g|kg|ml|l|gm|pack|pcs|piece|pieces))", re.IGNORECASE)
    match = pattern.search(text or "")
    return match.group(1) if match else None


def _extract_total_quantity(text: str) -> Optional[str]:
    txt = (text or "").lower().replace(" ", "")

    p1 = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)[x*](\d+)", txt)
    if p1:
        base = float(p1.group(1))
        unit = p1.group(2)
        count = float(p1.group(3))
        total = base * count
        if unit in {"kg", "l"}:
            total *= 1000
            unit = "g" if unit == "kg" else "ml"
        if float(total).is_integer():
            total = int(total)
        return f"{total}{'g' if unit in {'g', 'gm'} else unit}"

    p2 = re.search(r"(\d+)[x*](\d+(?:\.\d+)?)(g|gm|kg|ml|l)", txt)
    if p2:
        count = float(p2.group(1))
        base = float(p2.group(2))
        unit = p2.group(3)
        total = base * count
        if unit in {"kg", "l"}:
            total *= 1000
            unit = "g" if unit == "kg" else "ml"
        if float(total).is_integer():
            total = int(total)
        return f"{total}{'g' if unit in {'g', 'gm'} else unit}"

    return None


def _is_combo_hint(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in ["combo", "pack of", "x", "*"])


def _extract_pack_count(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"pack\s*of\s*(\d+)", text, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_pack_count_from_specs(soup: BeautifulSoup) -> Optional[int]:
    try:
        for row in soup.select("tr, li, div"):
            txt = row.get_text(" ", strip=True)
            low = txt.lower()
            if not txt:
                continue
            if any(k in low for k in ["sales package", "pack of", "number of contents", "quantity"]):
                m = re.search(r"pack\s*of\s*(\d+)", txt, flags=re.IGNORECASE)
                if m:
                    return int(m.group(1))
                m2 = re.search(r"(\d+)\s*[x*]\s*\d+\s*(?:g|gm|kg|ml|l)", txt, flags=re.IGNORECASE)
                if m2:
                    return int(m2.group(1))
    except Exception:
        return None
    return None


def scrape_product_data(url: str) -> Optional[Dict[str, Any]]:
    for timeout_s in (20, 30):
        try:
            response = requests.get(
                url,
                headers={**SCRAPE_HEADERS, "Referer": "https://www.google.com/"},
                timeout=timeout_s,
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            title = None
            price = None
            offers = []

            page_text = soup.get_text(" ", strip=True)

            if "amazon." in url:
                title_tag = soup.select_one("#productTitle")
                price_tag = soup.select_one(".a-price .a-offscreen") or soup.select_one("#priceblock_ourprice")
                offer_tags = soup.select(".a-size-base.a-color-price")

                title = title_tag.get_text(strip=True) if title_tag else None
                price = _parse_price(price_tag.get_text(strip=True)) if price_tag else None
                offers = [o.get_text(strip=True) for o in offer_tags[:3]]

            elif "flipkart.com" in url:
                title_tag = soup.select_one("span.B_NuCI") or soup.select_one("h1 span")
                price_tag = soup.select_one("div._30jeq3._16Jk6d") or soup.select_one("div.Nx9bqj")
                offer_tags = soup.select("div._3Ay6Sb span")

                title = title_tag.get_text(strip=True) if title_tag else None
                price = _parse_price(price_tag.get_text(strip=True)) if price_tag else None
                offers = [o.get_text(strip=True) for o in offer_tags[:3]]

                if price is None:
                    meta_price = soup.select_one("meta[property='product:price:amount']")
                    if meta_price and meta_price.get("content"):
                        price = _parse_price(meta_price["content"])

                if price is None:
                    json_price = re.search(r'"finalPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?', response.text)
                    if json_price:
                        price = float(json_price.group(1))

                if title is None:
                    og_title = soup.select_one("meta[property='og:title']")
                    if og_title and og_title.get("content"):
                        title = og_title["content"]

                if price is None:
                    ld_json_tags = soup.select("script[type='application/ld+json']")
                    for tag in ld_json_tags:
                        text = tag.get_text(strip=True)
                        if not title:
                            name_match = re.search(r'"name"\s*:\s*"([^"]+)"', text)
                            if name_match:
                                title = name_match.group(1)
                        price_match = re.search(r'"price"\s*:\s*"?(\\d+(?:\\.\\d+)?)"?', text)
                        if price_match:
                            price = float(price_match.group(1))
                            break

                if price is None:
                    rupee_match = re.search(r"₹\s*([0-9][0-9,]+(?:\.\d+)?)", page_text)
                    if rupee_match:
                        price = _parse_price(rupee_match.group(1))

            if not title or price is None:
                continue

            pack_count = _extract_pack_count(title) or _extract_pack_count_from_specs(soup)
            title_combo_qty = _extract_total_quantity(title) if _is_combo_hint(title or "") else None
            title_qty = _extract_quantity(title)
            page_qty = _extract_quantity(page_text)
            quantity = title_combo_qty or title_qty or page_qty

            return {
                "title": title,
                "price": price,
                "quantity": quantity,
                "pack_count": pack_count,
                "offers": offers,
                "url": url,
            }
        except Exception:
            continue
    return None
