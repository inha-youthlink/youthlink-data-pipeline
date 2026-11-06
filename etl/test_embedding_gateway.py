from __future__ import annotations

import os

from dotenv import load_dotenv

try:
    from .embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        request_embeddings,
    )
except ImportError:  # Direct execution: python etl/test_embedding_gateway.py
    from embedding_utils import (
        DEFAULT_EMBEDDING_DIMENSIONS,
        DEFAULT_EMBEDDING_MODEL,
        create_embedding_client,
        request_embeddings,
    )


def main() -> int:
    """Verify gateway auth, model access, and vector dimensions without DB I/O."""
    load_dotenv()
    api_key = os.getenv("LITELLM_API_KEY", "").strip()
    base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    model = os.getenv(
        "LITELLM_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
    ).strip()

    if not api_key:
        raise RuntimeError("LITELLM_API_KEY is not set in .env")
    if not base_url:
        raise RuntimeError("LITELLM_BASE_URL is not set in .env")

    client = create_embedding_client(api_key, base_url)
    vectors, usage_tokens = request_embeddings(
        client,
        ["청년 월세 지원 정책을 찾고 있습니다."],
        model=model,
        dimensions=DEFAULT_EMBEDDING_DIMENSIONS,
    )

    print(
        "embedding gateway test passed: "
        f"model={model}, dimensions={len(vectors[0])}, format=float, "
        f"usage_tokens={usage_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
