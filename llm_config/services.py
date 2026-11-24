import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from .models import LLMConfiguration

logger = logging.getLogger(__name__)


def get_active_llm_config() -> LLMConfiguration:
    """Fetch the active default LLM configuration from database (auto-seeding if empty)."""
    return LLMConfiguration.get_active_config()


def call_llm(
    prompt: str,
    system_prompt: Optional[str] = None,
    config: Optional[LLMConfiguration] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute an LLM prompt using the database configuration (no .env required).

    Returns a dict with:
        {"success": bool, "text": str, "model": str, "provider": str, "error": Optional[str]}
    """
    if config is None:
        config = get_active_llm_config()

    eff_system = (
        system_prompt
        if system_prompt is not None
        else (config.system_prompt or "You are an AI assistant for the Electricity Department.")
    )
    eff_temp = float(temperature if temperature is not None else config.temperature)
    eff_tokens = int(max_tokens if max_tokens is not None else config.max_tokens)
    provider = (config.provider or "gemini").lower()
    model_name = config.model_name or "gemini-1.5-flash"
    api_key = (config.api_key or "").strip()
    api_url = (config.api_url or "").strip()

    if not prompt or not prompt.strip():
        return {
            "success": False,
            "text": "",
            "model": model_name,
            "provider": provider,
            "error": "Empty prompt provided.",
        }

    try:
        # 1. Google Gemini (google-genai SDK)
        if provider == LLMConfiguration.PROVIDER_GEMINI:
            if not api_key:
                return {
                    "success": False,
                    "text": "",
                    "model": model_name,
                    "provider": provider,
                    "error": "Gemini API key is not configured in database. Please enter your API key in Admin Settings.",
                }
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key)
            # Combine system instruction if supported
            cfg = types.GenerateContentConfig(
                temperature=eff_temp,
                max_output_tokens=eff_tokens,
                system_instruction=eff_system if eff_system else None,
            )
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=cfg,
            )
            output_text = getattr(resp, "text", "") or ""
            return {
                "success": True,
                "text": output_text.strip(),
                "model": model_name,
                "provider": provider,
                "error": None,
            }

        # 2. OpenAI (GPT)
        elif provider == LLMConfiguration.PROVIDER_OPENAI:
            if not api_key:
                return {
                    "success": False,
                    "text": "",
                    "model": model_name,
                    "provider": provider,
                    "error": "OpenAI API key is not configured in database.",
                }
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=api_url if api_url else None)
            messages = []
            if eff_system:
                messages.append({"role": "system", "content": eff_system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=eff_temp,
                max_tokens=eff_tokens,
            )
            output_text = resp.choices[0].message.content or ""
            return {
                "success": True,
                "text": output_text.strip(),
                "model": model_name,
                "provider": provider,
                "error": None,
            }

        # 3. Groq (Groq SDK)
        elif provider == LLMConfiguration.PROVIDER_GROQ:
            if not api_key:
                return {
                    "success": False,
                    "text": "",
                    "model": model_name,
                    "provider": provider,
                    "error": "Groq API key is not configured in database.",
                }
            from groq import Groq

            client = Groq(api_key=api_key)
            messages = []
            if eff_system:
                messages.append({"role": "system", "content": eff_system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=eff_temp,
                max_tokens=eff_tokens,
            )
            output_text = resp.choices[0].message.content or ""
            return {
                "success": True,
                "text": output_text.strip(),
                "model": model_name,
                "provider": provider,
                "error": None,
            }

        # 4. Ollama (Local REST)
        elif provider == LLMConfiguration.PROVIDER_OLLAMA:
            import requests

            endpoint = api_url.rstrip("/") if api_url else "http://localhost:11434"
            target_url = f"{endpoint}/api/generate"
            payload = {
                "model": model_name,
                "prompt": prompt,
                "system": eff_system,
                "stream": False,
                "options": {
                    "temperature": eff_temp,
                    "num_predict": eff_tokens,
                },
            }
            res = requests.post(target_url, json=payload, timeout=20)
            res.raise_for_status()
            data = res.json()
            return {
                "success": True,
                "text": data.get("response", "").strip(),
                "model": model_name,
                "provider": provider,
                "error": None,
            }

        # 5. Custom OpenAI-compatible REST API
        elif provider == LLMConfiguration.PROVIDER_CUSTOM:
            from openai import OpenAI

            if not api_url:
                return {
                    "success": False,
                    "text": "",
                    "model": model_name,
                    "provider": provider,
                    "error": "Custom API endpoint URL is required.",
                }
            client = OpenAI(api_key=api_key or "custom", base_url=api_url)
            messages = []
            if eff_system:
                messages.append({"role": "system", "content": eff_system})
            messages.append({"role": "user", "content": prompt})
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=eff_temp,
                max_tokens=eff_tokens,
            )
            output_text = resp.choices[0].message.content or ""
            return {
                "success": True,
                "text": output_text.strip(),
                "model": model_name,
                "provider": provider,
                "error": None,
            }

        else:
            return {
                "success": False,
                "text": "",
                "model": model_name,
                "provider": provider,
                "error": f"Unsupported LLM provider: {provider}",
            }

    except Exception as exc:
        logger.exception("LLM call failed for provider %s: %s", provider, exc)
        return {
            "success": False,
            "text": "",
            "model": model_name,
            "provider": provider,
            "error": str(exc),
        }


def test_llm_connection(config_id: int) -> Dict[str, Any]:
    """Test connectivity for an LLM configuration saved in the database."""
    try:
        cfg = LLMConfiguration.objects.get(pk=config_id)
    except LLMConfiguration.DoesNotExist:
        return {"ok": False, "error": f"LLM Configuration with ID {config_id} does not exist."}

    if cfg.provider in (
        LLMConfiguration.PROVIDER_GEMINI,
        LLMConfiguration.PROVIDER_OPENAI,
        LLMConfiguration.PROVIDER_GROQ,
    ) and not cfg.api_key:
        return {
            "ok": False,
            "error": f"API key is missing for {cfg.get_provider_display()}. Enter your API key on the configuration card.",
            "provider": cfg.provider,
            "model": cfg.model_name,
        }

    test_prompt = "Ping test: Reply with exactly 'DISCOM LLM Ready'."
    res = call_llm(
        prompt=test_prompt,
        system_prompt="You are an automated DISCOM diagnostic tool. Be concise.",
        config=cfg,
        temperature=0.0,
        max_tokens=20,
    )

    if res["success"]:
        return {
            "ok": True,
            "message": f"Connection verified successfully with {cfg.get_provider_display()}!",
            "reply": res["text"] or "OK",
            "provider": cfg.provider,
            "model": cfg.model_name,
        }
    else:
        return {
            "ok": False,
            "error": res["error"] or "Unknown error while connecting to LLM provider.",
            "provider": cfg.provider,
            "model": cfg.model_name,
        }
