"""Generic Chat Completions transport, independent of bot/plugin startup."""
import httpx


async def complete(ai, messages, *, model=None, timeout=None, transport=None):
    request_model = model or ai["model"]
    options = {"timeout": timeout or ai.get("primary_timeout", 90), "trust_env": True}
    if transport is not None:
        options["transport"] = transport
    elif ai.get("proxy"):
        options["proxy"] = ai["proxy"]
    async with httpx.AsyncClient(**options) as client:
        response = await client.post(ai["api_url"],
            headers={"Authorization": "Bearer " + ai["api_key"]},
            json={"model": request_model, "messages": messages, "stream": False})
        if response.status_code != 200:
            # Do not echo upstream bodies or URLs: they can include prompts/keys.
            raise RuntimeError(f"Model endpoint returned HTTP {response.status_code}")
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise RuntimeError("Model endpoint returned an invalid Chat Completions response.") from None
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise RuntimeError("Model output was truncated or filtered.")
        if isinstance(content, list):
            content = "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Empty model response; check Chat Completions compatibility.")
        return content.strip(), data.get("usage") or {}
