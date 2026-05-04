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
    Load config from config.json with env-var fallback.
    """
    file_cfg = _read_json_config()

    return {
        "groq_api_key": file_cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY"),
        "groq_model": file_cfg.get("groq_model") or os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile",
        "mongo_uri": file_cfg.get("mongo_uri") or os.getenv("MONGO_URI") or "mongodb://localhost:27017/",
        "smtp_email": file_cfg.get("smtp_email") or os.getenv("SMTP_EMAIL"),
        "smtp_app_password": file_cfg.get("smtp_app_password") or os.getenv("SMTP_APP_PASSWORD"),
        "baileys_webhook_url": file_cfg.get("baileys_webhook_url") or os.getenv("BAILEYS_WEBHOOK_URL"),
        "baileys_to": file_cfg.get("baileys_to") or os.getenv("BAILEYS_TO"),
    }
