from __future__ import annotations

import math
import re
from typing import Any, Sequence
from urllib.parse import urlsplit


DEFAULT_EMBEDDING_MODEL = "bedrock-titan-embed-v2"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
_RETRY_SECONDS_RE = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)",
    re.IGNORECASE,
)


def create_embedding_client(
    api_key: str,
    base_url: str,
    timeout: float = 60.0,
) -> Any:
    """Create an OpenAI-compatible client for the configured LiteLLM gateway."""
    api_key = api_key.strip()
    base_url = base_url.strip()
    if not api_key:
        raise ValueError("embedding gateway API key must not be blank")
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("embedding gateway base URL is invalid") from exc
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(
            "embedding gateway base URL must be an absolute HTTPS URL"
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The openai package is required. Run: "
            "python -m pip install -r requirements.txt"
        ) from exc

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
    )


def estimate_token_count(text: str) -> int:
    """Return a dependency-free planning estimate, not a provider token count.

    Titan's tokenizer is not exposed by the OpenAI-compatible gateway. Treat
    each non-ASCII character as one token and each four ASCII characters as
    one token. Actual batch usage is read from the gateway response instead.
    """
    non_ascii_characters = sum(not character.isascii() for character in text)
    ascii_characters = len(text) - non_ascii_characters
    return max(non_ascii_characters + math.ceil(ascii_characters / 4), 1)


def vector_literal(vector: Sequence[float], dimensions: int) -> str:
    if len(vector) != dimensions:
        raise ValueError(
            f"Expected a {dimensions}-dimensional embedding, got {len(vector)}"
        )

    values: list[str] = []
    for value in vector:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Embedding contains a non-finite value")
        values.append(repr(number))
    return "[" + ",".join(values) + "]"


def reject_binary_vector(vector: Sequence[float]) -> None:
    """Fail before storage if the gateway returned binary (0/1) embeddings.

    A float embedding is never made of only 0 and 1. Seeing that means the
    request was served as Titan binary output (see request_embeddings).
    """
    if vector and all(value in (0.0, 1.0) for value in vector):
        raise ValueError(
            "Embedding API returned a binary (0/1) vector; "
            "request encoding_format='float'"
        )


def request_embeddings(
    client: Any,
    texts: Sequence[str],
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
) -> tuple[list[list[float]], int]:
    if not texts:
        return [], 0

    # The capstone gateway fixes bedrock-titan-embed-v2 at 1024 dimensions.
    # Do not send the provider-specific dimensions option: the documented
    # OpenAI-compatible request accepts only model and input. We still verify
    # every returned vector below so a gateway configuration mismatch fails
    # before anything reaches pgvector.
    #
    # encoding_format must be explicit. The OpenAI SDK sends "base64" when it
    # is omitted, and the gateway maps that to Titan's binary output, so every
    # dimension arrives as 0 or 1 instead of a float. Binary vectors keep only
    # about 1/32 of the information and compress cosine scores into a narrow
    # band, which breaks retrieval ranking.
    response = client.embeddings.create(
        input=list(texts),
        model=model,
        encoding_format="float",
    )
    data = sorted(response.data, key=lambda item: item.index)
    if len(data) != len(texts):
        raise RuntimeError(
            f"Embedding API returned {len(data)} vectors for {len(texts)} inputs"
        )

    vectors: list[list[float]] = []
    for expected_index, item in enumerate(data):
        if item.index != expected_index:
            raise RuntimeError(
                f"Embedding API response is missing input index {expected_index}"
            )
        vector = [float(value) for value in item.embedding]
        vector_literal(vector, dimensions)
        reject_binary_vector(vector)
        vectors.append(vector)

    usage = getattr(response, "usage", None)
    prompt_tokens = int(
        getattr(usage, "prompt_tokens", 0)
        or getattr(usage, "total_tokens", 0)
        or 0
    )
    return vectors, prompt_tokens


def is_retryable_error(error: Exception) -> bool:
    error_code = getattr(error, "code", None)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        error_code = error_code or body.get("code")
        nested = body.get("error")
        if isinstance(nested, dict):
            error_code = error_code or nested.get("code")

    # These can be returned with HTTP 429 but cannot succeed after waiting.
    if error_code in {
        "billing_not_active",
        "credit_balance_exhausted",
        "insufficient_quota",
        "budget_exceeded",
    }:
        return False

    status_code = getattr(error, "status_code", None)
    if status_code in {408, 409, 429}:
        return True
    if isinstance(status_code, int) and status_code >= 500:
        return True

    retryable_names = {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
    }
    return type(error).__name__ in retryable_names


def suggested_retry_delay(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except (TypeError, ValueError):
                pass

    match = _RETRY_SECONDS_RE.search(str(error))
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2).lower() == "ms":
        value /= 1000
    return max(value, 0.0)
