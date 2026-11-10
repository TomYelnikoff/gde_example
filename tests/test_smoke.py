"""Smoke tests that run in CI before every deployment.

Bedrock is mocked, so no AWS credentials or network access are needed.
Run locally:  python -m unittest discover -s tests -t . -v
"""
import json
import os
import re
import tempfile
import unittest
from unittest import mock

# Configure the app before importing it (temporary database, fixed secret, small limit).
_TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["FREE_MESSAGE_LIMIT"] = "2"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import app as appmod  # noqa: E402


def fake_stream(**_kwargs):
    return {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "pong"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 1}}},
    ])}


def events(response):
    return {e: json.loads(d) for e, d in re.findall(r"event: (\w+)\ndata: (.*)", response.get_data(as_text=True))}


_counter = 0


def new_user():
    """A logged-in test client for a brand-new account."""
    global _counter
    _counter += 1
    client = appmod.app.test_client()
    email = f"user{_counter}@example.com"
    r = client.post("/api/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201, r.get_data(as_text=True)
    return client, email


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    def test_health_is_public(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")

    def test_anonymous_users_are_kept_out(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        self.assertEqual(self.client.post("/api/chat", json={"message": "hi"}).status_code, 401)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/signup").status_code, 200)

    def test_signup_validation_login_logout(self):
        post = self.client.post
        self.assertEqual(post("/api/auth/signup", json={"email": "nope", "password": "password123"}).status_code, 400)
        self.assertEqual(post("/api/auth/signup", json={"email": "a@example.com", "password": "short"}).status_code, 400)
        self.assertEqual(post("/api/auth/signup", json={"email": "dup@example.com", "password": "password123"}).status_code, 201)
        self.assertEqual(post("/api/auth/signup", json={"email": "DUP@example.com", "password": "password123"}).status_code, 409)

        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(post("/api/auth/login", json={"email": "dup@example.com", "password": "wrong-password"}).status_code, 401)
        self.assertEqual(post("/api/auth/login", json={"email": "dup@example.com", "password": "password123"}).status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    @mock.patch.object(appmod, "_generate_title", return_value="A title")
    def test_chat_saves_history_tokens_and_enforces_free_limit(self, _title):
        client, _ = new_user()
        with mock.patch.object(appmod._bedrock, "converse_stream", side_effect=fake_stream) as bedrock:
            first = events(client.post("/api/chat", json={"message": "hello"}))
            self.assertEqual(first["saved"]["usage"], {"input_tokens": 5, "output_tokens": 1})
            self.assertEqual(first["saved"]["quota"]["remaining"], 1)
            cid = first["saved"]["conversation_id"]

            second = events(client.post("/api/chat", json={"message": "again", "conversation_id": cid}))
            self.assertEqual(second["saved"]["quota"]["remaining"], 0)
            self.assertEqual(bedrock.call_count, 2)

            blocked = client.post("/api/chat", json={"message": "one too many", "conversation_id": cid})
            self.assertEqual(blocked.status_code, 402)
            self.assertEqual(blocked.get_json()["code"], "limit_reached")
            self.assertEqual(bedrock.call_count, 2, "Bedrock must not be called once the limit is reached")

        conversation = client.get(f"/api/conversations/{cid}").get_json()
        self.assertEqual(len(conversation["messages"]), 4)
        self.assertEqual(conversation["usage"], {"input_tokens": 10, "output_tokens": 2})

    @mock.patch.object(appmod, "_generate_title", return_value="A title")
    def test_users_cannot_see_each_others_conversations(self, _title):
        owner, _ = new_user()
        with mock.patch.object(appmod._bedrock, "converse_stream", side_effect=fake_stream):
            cid = events(owner.post("/api/chat", json={"message": "private"}))["saved"]["conversation_id"]

        other, _ = new_user()
        self.assertEqual(other.get(f"/api/conversations/{cid}").status_code, 404)
        self.assertEqual(other.patch(f"/api/conversations/{cid}", json={"title": "x"}).status_code, 404)
        self.assertEqual(other.delete(f"/api/conversations/{cid}").status_code, 404)
        self.assertEqual(other.get("/api/conversations").get_json(), [])


if __name__ == "__main__":
    unittest.main()
