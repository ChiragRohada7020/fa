from datetime import UTC, datetime
import sys
from pathlib import Path
from typing import Any, Dict, List

from pymongo import MongoClient

# Ensure root path is importable for shared config.py
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from config import get_config


DB_NAME = "deal_finder"
COLLECTION_NAME = "products"


def _get_collection():
    cfg = get_config()
    client = MongoClient(cfg["mongo_uri"])
    db = client[DB_NAME]
    return db[COLLECTION_NAME]


def save_product(data: Dict[str, Any]) -> None:
    """Save parsed product with timestamp in MongoDB."""
    doc = {
        "product": data.get("product"),
        "quantity": data.get("quantity"),
        "target_price": data.get("target_price"),
        "created_at": datetime.now(UTC),
    }
    _get_collection().insert_one(doc)


def get_all_products() -> List[Dict[str, Any]]:
    """Return all tracked products."""
    return list(_get_collection().find({}, {"_id": 0}))
