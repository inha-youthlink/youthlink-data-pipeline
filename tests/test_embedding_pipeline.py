import unittest
import inspect
from types import SimpleNamespace

from etl.backfill_embedding_token_estimates import calculate_updates
from etl.embedding_utils import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    DEFAULT_EMBEDDING_ENCODING_FORMAT,
    DEFAULT_EMBEDDING_MODEL,
    create_embedding_client,
    estimate_token_count,
    is_retryable_error,
    reject_binary_vector,
    request_embeddings,
    suggested_retry_delay,
    vector_literal,
)
from etl.embed_policy_chunks import load_candidates, store_failure
from etl.embed_policy_web_chunks import (
    load_candidates as load_web_candidates,
    store_failure as store_web_failure,
)
from etl.search_policy_chunks import search_chunks


class FakeEmbeddings:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    def cursor(self, **kwargs):
        return self.fake_cursor


class EmbeddingUtilsTests(unittest.TestCase):
    def test_defaults_use_titan_gateway_alias_and_1024_dimensions(self):
        self.assertEqual(DEFAULT_EMBEDDING_MODEL, "bedrock-titan-embed-v2")
        self.assertEqual(DEFAULT_EMBEDDING_DIMENSIONS, 1024)
        self.assertEqual(DEFAULT_EMBEDDING_ENCODING_FORMAT, "float")

    def test_gateway_client_rejects_missing_or_relative_base_url(self):
        with self.assertRaisesRegex(ValueError, "API key"):
            create_embedding_client("", "https://gateway.example/v1")
        with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
            create_embedding_client("secret", "/v1")
        with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
            create_embedding_client("secret", "http://gateway.example/v1")

    def test_local_token_estimate_never_downloads_a_tokenizer(self):
        self.assertEqual(estimate_token_count("abcd"), 1)
        self.assertEqual(estimate_token_count("청년 abcd"), 4)
        self.assertEqual(estimate_token_count(""), 1)

    def test_token_estimate_backfill_updates_only_mismatched_rows(self):
        rows = [
            {"chunk_id": "a", "content": "abcd", "token_count": 99},
            {"chunk_id": "b", "content": "청년", "token_count": 2},
        ]

        updates = calculate_updates(rows, "chunk_id")

        self.assertEqual(updates, [(1, "a")])

    def test_request_embeddings_restores_input_order_and_usage(self):
        response = SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[-0.25, 0.75]),
                SimpleNamespace(index=0, embedding=[0.125, -0.5]),
            ],
            usage=SimpleNamespace(prompt_tokens=7),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        vectors, tokens = request_embeddings(
            client, ["첫째", "둘째"], model="test-model", dimensions=2
        )

        self.assertEqual(vectors, [[0.125, -0.5], [-0.25, 0.75]])
        self.assertEqual(tokens, 7)
        self.assertEqual(client.embeddings.calls[0]["input"], ["첫째", "둘째"])
        self.assertEqual(client.embeddings.calls[0]["model"], "test-model")
        self.assertEqual(client.embeddings.calls[0]["encoding_format"], "float")
        self.assertNotIn("dimensions", client.embeddings.calls[0])

    def test_request_embeddings_asks_for_float_encoding(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.6, 0.8])],
            usage=SimpleNamespace(prompt_tokens=1),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        request_embeddings(client, ["text"], dimensions=2)

        # Without it the SDK sends base64 and the gateway returns binary 0/1.
        self.assertEqual(client.embeddings.calls[0]["encoding_format"], "float")

    def test_request_embeddings_rejects_binary_vectors(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[1, 0, 0, 1])],
            usage=SimpleNamespace(prompt_tokens=1),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        with self.assertRaisesRegex(ValueError, "binary"):
            request_embeddings(client, ["text"], dimensions=4)

    def test_reject_binary_vector_accepts_float_values(self):
        reject_binary_vector([0.031, -0.0127, 1.0, 0.0])

    def test_request_embeddings_accepts_total_tokens_usage(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.1, -0.2])],
            usage=SimpleNamespace(total_tokens=3),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        _, tokens = request_embeddings(client, ["text"], dimensions=2)

        self.assertEqual(tokens, 3)

    def test_request_embeddings_rejects_wrong_dimension(self):
        response = SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[1.0])],
            usage=SimpleNamespace(prompt_tokens=1),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        with self.assertRaisesRegex(ValueError, "2-dimensional"):
            request_embeddings(client, ["text"], dimensions=2)

    def test_vector_literal_rejects_non_finite_values(self):
        with self.assertRaisesRegex(ValueError, "non-finite"):
            vector_literal([float("nan")], 1)

    def test_retryable_error_uses_status_code(self):
        retryable = RuntimeError("temporary")
        retryable.status_code = 429
        permanent = RuntimeError("bad request")
        permanent.status_code = 400

        self.assertTrue(is_retryable_error(retryable))
        self.assertFalse(is_retryable_error(permanent))

    def test_credit_exhaustion_is_not_retried_even_when_status_is_429(self):
        error = RuntimeError("no credits")
        error.status_code = 429
        error.body = {"code": "credit_balance_exhausted"}

        self.assertFalse(is_retryable_error(error))

    def test_retry_delay_is_read_from_rate_limit_message(self):
        error = RuntimeError("Please try again in 8.523s")
        self.assertEqual(suggested_retry_delay(error), 8.523)

    def test_retry_delay_supports_milliseconds(self):
        error = RuntimeError("Please try again in 750ms")
        self.assertEqual(suggested_retry_delay(error), 0.75)


class VectorSearchTests(unittest.TestCase):
    def test_failed_refresh_clears_incomplete_vector_lineage(self):
        for function in (store_failure, store_web_failure):
            source = inspect.getsource(function)
            self.assertIn("embedding = NULL", source)
            self.assertIn("embedding_model = NULL", source)
            self.assertIn("embedding_encoding_format = NULL", source)
            self.assertIn("embedded_at = NULL", source)

    def test_embedding_candidates_exclude_inactive_policies(self):
        cursor = FakeCursor([])
        conn = FakeConnection(cursor)

        candidates, estimated_tokens = load_candidates(
            conn,
            model="test-model",
            limit=None,
            retry_errors=False,
            force=False,
        )

        self.assertEqual(candidates, [])
        self.assertEqual(estimated_tokens, 0)
        self.assertIn("JOIN policy p ON p.policy_no = pc.policy_no", cursor.sql)
        self.assertIn("p.is_active = TRUE", cursor.sql)
        self.assertIn("pc.embedding IS NULL", cursor.sql)
        self.assertIn("pc.embedding_encoding_format IS DISTINCT FROM %s", cursor.sql)
        self.assertEqual(cursor.params[:2], ["test-model", "float"])

    def test_web_embedding_candidates_use_only_verified_separate_corpus(self):
        cursor = FakeCursor([])
        conn = FakeConnection(cursor)

        candidates, estimated_tokens = load_web_candidates(
            conn,
            model="test-model",
            policy_nos=["P1"],
            limit=None,
            retry_errors=False,
            force=False,
        )

        self.assertEqual(candidates, [])
        self.assertEqual(estimated_tokens, 0)
        self.assertIn("FROM policy_web_chunk AS chunk", cursor.sql)
        self.assertIn("JOIN v_verified_policy_web_chunk AS verified", cursor.sql)
        self.assertIn("verified.policy_no = ANY(%s)", cursor.sql)
        self.assertIn(
            "chunk.embedding_encoding_format IS DISTINCT FROM %s", cursor.sql
        )
        self.assertEqual(cursor.params[:2], ["test-model", "float"])
        self.assertNotIn("FROM policy_chunk", cursor.sql)

    def test_search_uses_cosine_operator_and_candidate_filters(self):
        cursor = FakeCursor(
            [
                {
                    "policy_no": "P1",
                    "policy_name": "정책",
                    "chunk_type": "SUPPORT",
                    "content": "지원",
                    "similarity": 0.9,
                }
            ]
        )
        conn = FakeConnection(cursor)

        rows = search_chunks(
            conn,
            [0.0] * 1023 + [1.0],
            model="test-model",
            limit=5,
            policy_nos=["P1"],
            chunk_types=["SUPPORT"],
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("pc.embedding <=> %s::vector", cursor.sql)
        self.assertIn("p.is_active = TRUE", cursor.sql)
        self.assertEqual(
            cursor.params[1:5],
            ["test-model", "float", ["P1"], ["SUPPORT"]],
        )
        self.assertEqual(cursor.params[-1], 5)
        self.assertEqual(cursor.params[0], cursor.params[-2])


if __name__ == "__main__":
    unittest.main()
