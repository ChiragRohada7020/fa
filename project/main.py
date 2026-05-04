import os

from ai_parser import parse_user_input
from db import save_product
from scheduler import start_scheduler


def main() -> None:
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        raise EnvironmentError("Set GROQ_API_KEY environment variable before running.")

    user_input = input("Enter product request: ").strip()
    if not user_input:
        print("No input provided. Exiting.")
        return

    parsed = parse_user_input(user_input, groq_api_key)
    if not parsed.get("product"):
        print("Could not parse a valid product from input.")
        return

    save_product(parsed)
    print(
        f"Saved product:\n{parsed.get('product')} "
        f"{parsed.get('quantity') or ''} "
        f"target ₹{parsed.get('target_price')}"
    )

    print("\nStarting scheduler (checks every 2 hours)...")
    start_scheduler()


if __name__ == "__main__":
    main()
