import re
import os
from typing import List
from urllib.parse import quote_plus
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    if "flipkart.com" in parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if "amazon.in" in parsed.netloc:
        # Keep canonical product path; drop tracking query.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return url


def _score_url(url: str, product_query: str) -> int:
    """
    Basic relevance score from URL slug vs query tokens.
    """
    slug = unquote(urlparse(url).path).lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", product_query.lower()) if len(t) > 2]
    return sum(1 for t in tokens if t in slug)


def _bing_search(product_query: str, max_results: int) -> List[str]:
    query = quote_plus(f"{product_query} site:amazon.in OR site:flipkart.com")
    url = f"https://www.bing.com/search?q={query}"

    response = requests.get(url, headers=SEARCH_HEADERS, timeout=8)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    urls: List[str] = []
    for link in soup.select("li.b_algo h2 a"):
        href = link.get("href", "")
        if "amazon.in" in href or "flipkart.com" in href:
            urls.append(_clean_url(href))
        if len(urls) >= max_results:
            break
    return urls


def _flipkart_site_search(product_query: str, max_results: int) -> List[str]:
    query = quote_plus(product_query)
    urls: List[str] = []
    # Crawl a few pages because relevant variants may not appear on page 1.
    starts = [0, 24, 48, 72, 96, 120, 144, 168, 192]
    for start in starts:
        url = f"https://www.flipkart.com/search?q={query}&start={start}"
        response = requests.get(url, headers=SEARCH_HEADERS, timeout=8)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for a_tag in soup.select("a[href*='/p/']"):
            href = a_tag.get("href", "")
            if not href:
                continue
            full = urljoin("https://www.flipkart.com", unquote(href))
            if "/p/" not in full:
                continue
            cleaned = _clean_url(full)
            if cleaned not in urls:
                urls.append(cleaned)
            if len(urls) >= max_results:
                return urls
    return urls


def _amazon_site_search(product_query: str, max_results: int) -> List[str]:
    query = quote_plus(product_query)
    url = f"https://www.amazon.in/s?k={query}"
    response = requests.get(url, headers=SEARCH_HEADERS, timeout=8)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    urls: List[str] = []
    for a_tag in soup.select("a[href*='/dp/']"):
        href = a_tag.get("href", "")
        if not href:
            continue
        full = urljoin("https://www.amazon.in", href)
        if "/dp/" not in full:
            continue
        urls.append(_clean_url(full))
        if len(urls) >= max_results:
            break
    return urls


def search_product_urls(product_query: str, max_results: int = 5) -> List[str]:
    """
    Search product pages using Bing + direct Amazon/Flipkart search pages.
    """
    merged: List[str] = []
    seen = set()

    use_amazon = os.getenv("ENABLE_AMAZON_SEARCH", "0").strip().lower() in {"1", "true", "yes"}
    providers = [_flipkart_site_search, _bing_search]
    if use_amazon:
        providers.insert(1, _amazon_site_search)

    # Prefer Flipkart first for speed/stability.
    for provider in providers:
        try:
            # Pull a wider pool from each provider before final ranking.
            found = provider(product_query, max_results * 4)
        except Exception:
            found = []

        for url in found:
            if url in seen:
                continue
            seen.add(url)
            merged.append(url)

    # Rank by URL-query relevance; keep only requested size.
    # Deterministic ordering: relevance first, URL tie-break next.
    merged.sort(key=lambda u: (-_score_url(u, product_query), u))
    return merged[:max_results]
