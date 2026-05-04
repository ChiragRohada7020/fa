import json
import re
from typing import Any, Dict, Optional

import requests


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def _build_prompt(user_text: str) -> str:
    return f"""
Extract product details from this messy shopping request and return ONLY JSON.

Input: "{user_text}"

Required JSON format:
{{
  "product": "string",
  "quantity": "string or null",
  "target_price": number or null
}}

Rules:
- Product name should be clean and human-readable.
- Quantity examples: "150g", "2kg", "1L", "pack of 2".
- target_price is the maximum budget mentioned (e.g. 'under 50' => 50).
- Return only valid JSON, no markdown, no explanations.
""".strip()


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from model output."""
    text = text.strip()

    # Try direct JSON parse first.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: capture first JSON object in response.
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in Groq response.")

    return json.loads(match.group(0))


def parse_user_input(user_text: str, groq_api_key: str, model: str = "llama-3.3-70b-versatile") -> Dict[str, Any]:
    """
    Parse messy user text into:
    {
      "product": str,
      "quantity": Optional[str],
      "target_price": Optional[float]
    }

    Retries once if Groq call fails.
    """
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json",
    }
    # Try the selected model first, then a safe fallback.
    model_candidates = [model, "llama3-8b-8192"]

    last_error: Optional[Exception] = None
    for idx, model_name in enumerate(model_candidates[:2]):  # one retry across models
        payload = {
            "model": model_name,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You are an information extraction engine."},
                {"role": "user", "content": _build_prompt(user_text)},
            ],
        }
        try:
            response = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {response.status_code} from Groq (model={model_name}): {response.text[:300]}"
                )
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = _extract_json(content)

            return {
                "product": parsed.get("product"),
                "quantity": parsed.get("quantity"),
                "target_price": parsed.get("target_price"),
            }
        except Exception as exc:
            last_error = exc
            if idx == 0:
                continue

    raise RuntimeError(f"Groq parsing failed after retry: {last_error}")
