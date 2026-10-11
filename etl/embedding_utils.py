from __future__ import annotations

import math
import re
from typing import Any, Sequence


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 1536
TOKEN_ENCODING = "cl100k_base"
_RETRY_SECONDS_RE = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)",
    re.IGNORECASE,
)


def create_openai_client(api_key: str, timeout: float = 60.0) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The openai package is required. Run: "
            "python -m pip install -r requirements.txt"
        ) from exc

    return OpenAI(api_key=api_key, timeout=timeout, max_retries=0)


def get_token_encoder() -> Any:
    try:
        import tiktoken
    except ImportError as exc:
        raise RuntimeError(
            "The tiktoken package is required. Run: "
            "python -m pip install -r requirements.txt"
        ) from exc
    return tiktoken.get_encoding(TOKEN_ENCODING)


def count_tokens(text: str, encoder: Any) -> int:
    return len(encoder.encode(text))


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


def request_embeddings(
    client: Any,
    texts: Sequence[str],
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS,
) -> tuple[list[list[float]], int]:
    if not texts:
        return [], 0

    response = client.embeddings.create(
        input=list(texts),
        model=model,
        dimensions=dimensions,
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
        vectors.append(vector)

    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
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
