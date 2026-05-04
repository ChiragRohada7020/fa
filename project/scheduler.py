import time
from typing import Any, Dict

from db import get_all_products
from price_engine import calculate_price_metrics
from scraper import scrape_product_data
from search import search_product_urls


def run_price_check_once() -> None:
    products = get_all_products()
    if not products:
        print("No products in DB yet.")
        return

    for product in products:
        product_name = product.get("product")
        quantity = product.get("quantity")
        target_price = product.get("target_price")
        query = f"{product_name} {quantity or ''}".strip()

        print(f"\nChecking: {query} | Target: ₹{target_price}")

        try:
            urls = search_product_urls(query)
        except Exception as exc:
            print(f"Search failed for {query}: {exc}")
            continue

        if not urls:
            print("No product links found.")
            continue

        found_deal = False
        for url in urls:
            scraped = scrape_product_data(url)
            if not scraped:
                continue

            metrics = calculate_price_metrics(scraped)
            final_price = metrics["final_price"]
            per_unit = metrics["price_per_unit"]

            print(f"Title: {scraped['title']}")
            print(f"Base Price: {metrics['base_price']}")
            print(f"Final Price: {final_price}")
            print(f"Per Unit: {per_unit if per_unit is not None else 'N/A'}")
            print(f"URL: {url}")

            # Deal rule as requested
            if target_price is not None and per_unit is not None and per_unit <= float(target_price):
                print("🔥 DEAL FOUND")
                found_deal = True
                break
            else:
                print("❌ No deal")

        if not found_deal:
            print("No deal yet")


def start_scheduler() -> None:
    """Run deal checks forever every 2 hours."""
    while True:
        print("\n--- Running scheduled price check ---")
        run_price_check_once()
        print("--- Sleeping for 2 hours ---")
        time.sleep(7200)
