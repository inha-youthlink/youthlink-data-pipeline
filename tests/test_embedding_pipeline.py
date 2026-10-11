import unittest
from types import SimpleNamespace

from etl.embedding_utils import (
    is_retryable_error,
    request_embeddings,
    suggested_retry_delay,
    vector_literal,
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
    def test_request_embeddings_restores_input_order_and_usage(self):
        response = SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                SimpleNamespace(index=0, embedding=[1.0, 0.0]),
            ],
            usage=SimpleNamespace(prompt_tokens=7),
        )
        client = SimpleNamespace(embeddings=FakeEmbeddings(response))

        vectors, tokens = request_embeddings(
            client, ["첫째", "둘째"], model="test-model", dimensions=2
        )

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(tokens, 7)
        self.assertEqual(client.embeddings.calls[0]["input"], ["첫째", "둘째"])
        self.assertEqual(client.embeddings.calls[0]["dimensions"], 2)

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
            [0.0] * 1535 + [1.0],
            model="test-model",
            limit=5,
            policy_nos=["P1"],
            chunk_types=["SUPPORT"],
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("pc.embedding <=> %s::vector", cursor.sql)
        self.assertEqual(cursor.params[1:4], ["test-model", ["P1"], ["SUPPORT"]])
        self.assertEqual(cursor.params[-1], 5)
        self.assertEqual(cursor.params[0], cursor.params[-2])


if __name__ == "__main__":
    unittest.main()
