import json
import os
from pathlib import Path
from typing import Any, Dict


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def _read_json_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_config() -> Dict[str, Any]:
    """
    Load config with environment variables first (best for deployment),
    then fallback to config.json for local convenience.
    """
    file_cfg = _read_json_config()

    return {
        "groq_api_key": os.getenv("GROQ_API_KEY") or file_cfg.get("groq_api_key"),
        "groq_model": os.getenv("GROQ_MODEL") or file_cfg.get("groq_model") or "llama-3.3-70b-versatile",
        "mongo_uri": os.getenv("MONGO_URI") or file_cfg.get("mongo_uri") or "mongodb://localhost:27017/",
        "smtp_email": os.getenv("SMTP_EMAIL") or file_cfg.get("smtp_email"),
        "smtp_app_password": os.getenv("SMTP_APP_PASSWORD") or file_cfg.get("smtp_app_password"),
        "baileys_webhook_url": os.getenv("BAILEYS_WEBHOOK_URL") or file_cfg.get("baileys_webhook_url"),
        "baileys_to": os.getenv("BAILEYS_TO") or file_cfg.get("baileys_to"),
    }
