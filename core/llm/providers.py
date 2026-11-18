"""LLM Provider implementations using plain requests.

Supported providers:
- gemini: Direct REST call to Generative Language API using GEMINI_API_KEY
- openai_compat: Chat completions endpoint (OpenAI, DeepSeek, Groq, OpenRouter, Ollama)
- fake: In-memory test provider using FakeLLM registry
- litellm: Optional, only loaded if package is already installed (not installed by default)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


class LLMError(Exception):
    """Base exception for all LLM gateway errors."""
    pass


class RetryableLLMError(LLMError):
    """Exception for transient errors (rate limit, timeout, server errors) that should be retried."""
    pass


def _call_gemini_rest(
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    timeout_s: int = 30,
    expect_json: bool = False,
    api_key: Optional[str] = None,
) -> Tuple[str, int, int]:
    """Execute REST call to Google Gemini generateContent."""
    load_dotenv()
    key = api_key or os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMError("GEMINI_API_KEY is not set in environment or .env")

    # Format model name (e.g. models/gemini-2.5-flash)
    clean_model = model.replace("models/", "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={key}"

    # Separate system instruction from user/model contents
    system_instruction = None
    contents = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_instruction = {"parts": [{"text": content}]}
        else:
            gemini_role = "model" if role in ("assistant", "model") else "user"
            contents.append({"role": gemini_role, "parts": [{"text": content}]})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    generation_config: Dict[str, Any] = {
        "temperature": temperature,
    }
    if expect_json:
        generation_config["responseMimeType"] = "application/json"

    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation_config,
    }
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
        )
    except (requests.Timeout, requests.ConnectionError) as e:
        raise RetryableLLMError(f"Gemini connection/timeout error: {e}") from e

    if resp.status_code == 429:
        raise RetryableLLMError(f"Gemini rate limit (429): {resp.text}")
    if resp.status_code >= 500:
        raise RetryableLLMError(f"Gemini server error ({resp.status_code}): {resp.text}")
    if resp.status_code != 200:
        raise LLMError(f"Gemini API error ({resp.status_code}): {resp.text}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise LLMError(f"Gemini returned empty candidates: {data}")

    content_obj = candidates[0].get("content", {})
    parts = content_obj.get("parts", [])
    if not parts or "text" not in parts[0]:
        raise LLMError(f"Gemini returned no text parts: {data}")

    text = parts[0]["text"]
    usage = data.get("usageMetadata", {})
    tokens_in = int(usage.get("promptTokenCount", 0))
    tokens_out = int(usage.get("candidatesTokenCount", 0))

    return text, tokens_in, tokens_out


def _call_openai_compat_rest(
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    timeout_s: int = 30,
    expect_json: bool = False,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Tuple[str, int, int]:
    """Execute chat completions against an OpenAI-compatible endpoint."""
    load_dotenv()
    url_base = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    url = f"{url_base.rstrip('/')}/chat/completions"
    key = api_key or os.getenv("OPENAI_API_KEY", "")

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if expect_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout_s)
    except (requests.Timeout, requests.ConnectionError) as e:
        raise RetryableLLMError(f"OpenAI-compat connection/timeout error: {e}") from e

    if resp.status_code == 429:
        raise RetryableLLMError(f"OpenAI-compat rate limit (429): {resp.text}")
    if resp.status_code >= 500:
        raise RetryableLLMError(f"OpenAI-compat server error ({resp.status_code}): {resp.text}")
    if resp.status_code != 200:
        raise LLMError(f"OpenAI-compat API error ({resp.status_code}): {resp.text}")

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise LLMError(f"OpenAI-compat returned no choices: {data}")

    text = choices[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})
    tokens_in = int(usage.get("prompt_tokens", 0))
    tokens_out = int(usage.get("completion_tokens", 0))

    return text, tokens_in, tokens_out


def _call_fake(
    agent: str,
    model: str,
    messages: List[Dict[str, str]],
    expect_json: bool = False,
) -> Tuple[str, int, int]:
    """In-memory fake provider for automated unit tests."""
    from core.testing import FakeLLM
    return FakeLLM.get_instance().call(agent, model, messages, expect_json=expect_json)


def _call_litellm(
    model: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    timeout_s: int = 30,
    expect_json: bool = False,
    **kwargs: Any,
) -> Tuple[str, int, int]:
    """Optional litellm integration: only loaded if litellm package is already installed."""
    try:
        import litellm  # type: ignore
    except ImportError:
        raise LLMError(
            "litellm backend requested but litellm package is not installed. "
            "Please use gemini or openai_compat providers."
        )

    response_format = {"type": "json_object"} if expect_json else None
    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout=timeout_s,
            response_format=response_format,
            **kwargs,
        )
    except Exception as e:
        raise LLMError(f"litellm completion failed: {e}") from e

    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
    return text, tokens_in, tokens_out


def execute_provider_call(
    provider: str,
    model: str,
    agent: str,
    messages: List[Dict[str, str]],
    *,
    temperature: float = 0.7,
    timeout_s: int = 30,
    expect_json: bool = False,
    **kwargs: Any,
) -> Tuple[str, int, int]:
    """Route call to the selected provider."""
    provider_clean = provider.lower()
    if provider_clean == "gemini":
        return _call_gemini_rest(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout_s=timeout_s,
            expect_json=expect_json,
            api_key=kwargs.get("api_key"),
        )
    elif provider_clean in ("openai_compat", "openai"):
        return _call_openai_compat_rest(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout_s=timeout_s,
            expect_json=expect_json,
            base_url=kwargs.get("base_url"),
            api_key=kwargs.get("api_key"),
        )
    elif provider_clean == "fake":
        return _call_fake(
            agent=agent,
            model=model,
            messages=messages,
            expect_json=expect_json,
        )
    elif provider_clean == "litellm":
        return _call_litellm(
            model=model,
            messages=messages,
            temperature=temperature,
            timeout_s=timeout_s,
            expect_json=expect_json,
            **kwargs,
        )
    else:
        raise LLMError(f"Unsupported provider: {provider}")
