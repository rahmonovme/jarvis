# core/gemini_client.py
# Shared Gemini client factory — uses the NEW google.genai package.
# All project files should use this instead of the deprecated google.generativeai.

import json
import sys
from pathlib import Path
from datetime import date
from typing import List

_FALLBACK_CHAIN = [
    "gemini-2.5-flash-lite",         # Priority 1: High speed benchmark
    "gemini-2.5-flash",              # Priority 2: Standard parallel bucket (20 RPD)
    "gemini-3.1-flash-lite-preview", # Priority 3: Deep quota bucket (500 RPD)
    "gemini-flash-lite-latest"       # Priority 4: Universal fallback
]


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
API_LIMITS_PATH = BASE_DIR / "config" / "api_limits.json"

def get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _get_available_models(requested_model: str) -> List[str]:
    """Filters out models whose limit was reached today."""
    models_to_try = [requested_model]
    if requested_model in _FALLBACK_CHAIN:
        models_to_try = _FALLBACK_CHAIN[_FALLBACK_CHAIN.index(requested_model):].copy()

    try:
        if API_LIMITS_PATH.exists():
            with open(API_LIMITS_PATH, "r", encoding="utf-8") as f:
                limits = json.load(f)
        else:
            limits = {}
            
        today_str = date.today().isoformat()
        cleaned = False
        
        stale_keys = [m for m, d in limits.items() if d != today_str]
        for k in stale_keys:
            del limits[k]
            cleaned = True
            
        if cleaned:
            with open(API_LIMITS_PATH, "w", encoding="utf-8") as f:
                json.dump(limits, f, indent=4)
                
        filtered = [m for m in models_to_try if m not in limits]
        if not filtered:
            return models_to_try
        return filtered
    except Exception:
        return models_to_try

def _mark_model_exhausted(model: str):
    """Saves the exhausted model to the limits file."""
    try:
        limits = {}
        if API_LIMITS_PATH.exists():
            with open(API_LIMITS_PATH, "r", encoding="utf-8") as f:
                limits = json.load(f)
                
        limits[model] = date.today().isoformat()
        
        with open(API_LIMITS_PATH, "w", encoding="utf-8") as f:
            json.dump(limits, f, indent=4)
    except Exception:
        pass


def ask(prompt: str, model: str = "gemini-2.5-flash-lite",
        system_instruction: str = None) -> str:
    """Simple one-shot text generation. Returns response text."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=get_api_key())

    config = None
    if system_instruction:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction
        )

    # Smart-resolve starting point in fallback chain bypassing exhausted endpoints
    models_to_try = _get_available_models(model)

    last_err = None
    for m in models_to_try:
        try:
            response = client.models.generate_content(
                model=m,
                contents=prompt,
                config=config,
            )
            return response.text.strip()
        except Exception as e:
            last_err = e
            err_msg = str(e).lower()
            if "429" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                _mark_model_exhausted(m)
                if m != models_to_try[-1]:
                    print(f"[GeminiClient] ⚠️ {m} limit reached! Memorized exhaustion in storage. Auto-switching bucket...")
                continue
            raise e
            
    raise last_err


def ask_with_image(prompt: str, image_data: bytes,
                   mime_type: str = "image/png",
                   model: str = "gemini-2.5-flash-lite") -> str:
    """One-shot generation with an image input."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=get_api_key())

    contents = [
        types.Part.from_bytes(data=image_data, mime_type=mime_type),
        prompt,
    ]

    models_to_try = _get_available_models(model)

    last_err = None
    for m in models_to_try:
        try:
            response = client.models.generate_content(
                model=m,
                contents=contents,
            )
            return response.text.strip()
        except Exception as e:
            last_err = e
            err_msg = str(e).lower()
            if "429" in err_msg or "quota" in err_msg or "exhausted" in err_msg:
                _mark_model_exhausted(m)
                if m != models_to_try[-1]:
                    print(f"[GeminiClient] ⚠️ {m} limit reached! Memorized exhaustion in storage. Auto-switching bucket...")
                continue
            raise e

    raise last_err
