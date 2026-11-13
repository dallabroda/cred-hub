from __future__ import annotations

import datetime as dt
import unittest
from typing import Any

from cred_hub.storage.s3_client import S3Client, S3Config, S3StorageError, encode_s3_uri_path


class FakeS3Response:
	def __init__(self, status_code: int, content: bytes = b"", headers: dict[str, str] | None = None):
		self.status_code = status_code
		self.content = content
		self.headers = headers or {}
		self.closed = False

	def iter_content(self, chunk_size: int = 65536):
		for idx in range(0, len(self.content), chunk_size):
			yield self.content[idx : idx + chunk_size]

	def close(self):
		self.closed = True


class FakeS3Session:
	def __init__(self):
		self.store: dict[str, tuple[bytes, dict[str, str]]] = {}
		self.calls: list[tuple[str, str, dict[str, str]]] = []
		self.fail_next_503 = 0

	def request(self, method: str, url: str, data=None, headers=None, timeout=None, stream=False):
		hdrs = dict(headers or {})
		self.calls.append((method, url, hdrs))
		if self.fail_next_503 > 0:
			self.fail_next_503 -= 1
			return FakeS3Response(503, b"SlowDown")

		path = url.split(".amazonaws.com", 1)[1].split("?", 1)[0]
		if method == "PUT":
			body = data or b""
			meta = {
				"Content-Length": str(len(body)),
				"Content-Type": hdrs.get("content-type", "application/octet-stream"),
				"ETag": '"fake-etag-1"',
			}
			self.store[path] = (body, meta)
			return FakeS3Response(200, b"", meta)
		if method == "HEAD":
			if path not in self.store:
				return FakeS3Response(404)
			return FakeS3Response(200, b"", self.store[path][1])
		if method == "GET":
			if path not in self.store:
				return FakeS3Response(404)
			body, meta = self.store[path]
			return FakeS3Response(200, body, meta)
		if method == "DELETE":
			self.store.pop(path, None)
			return FakeS3Response(204)
		return FakeS3Response(405)


class TestS3Client(unittest.TestCase):
	def setUp(self):
		self.cfg = S3Config(
			bucket="credtech-hubtech-files-prod",
			region="us-east-1",
			access_key_id="AKIAEXAMPLE123",
			secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
			prefix="sites",
		)
		self.session = FakeS3Session()
		self.client = S3Client(self.cfg, session=self.session)

	def test_encode_uri_path_rejects_traversal_and_encodes_spaces(self):
		with self.assertRaises(S3StorageError):
			encode_s3_uri_path("sites/../etc/passwd")
		with self.assertRaises(S3StorageError):
			encode_s3_uri_path("")
		self.assertEqual(
			encode_s3_uri_path("sites/acme.credtechdigital.com.br/private/files/contrato social (1).pdf"),
			"/sites/acme.credtechdigital.com.br/private/files/contrato%20social%20%281%29.pdf",
		)

	def test_put_head_get_stream_and_delete_roundtrip(self):
		key = "sites/credtech.credtechdigital.com.br/private/files/dossie_123.pdf"
		payload = b"%PDF-1.4 sample dossier bytes"

		res = self.client.put_object(key, payload, content_type="application/pdf")
		self.assertEqual(res["size"], len(payload))

		# Must send AES256 server-side encryption header
		_, _, put_headers = self.session.calls[0]
		self.assertEqual(put_headers.get("x-amz-server-side-encryption"), "AES256")
		self.assertIn("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE123/", put_headers.get("Authorization", ""))

		head = self.client.head_object(key)
		self.assertIsNotNone(head)
		self.assertEqual(head["content_length"], len(payload))
		self.assertEqual(head["content_type"], "application/pdf")

		got = self.client.get_object(key)
		self.assertEqual(got, (payload, "application/pdf"))

		streamed = self.client.stream_object(key)
		self.assertIsNotNone(streamed)
		resp, ctype, length = streamed
		self.assertEqual(ctype, "application/pdf")
		self.assertEqual(length, len(payload))
		self.assertEqual(b"".join(resp.iter_content(8)), payload)

		self.assertTrue(self.client.delete_object(key))
		self.assertIsNone(self.client.head_object(key))
		self.assertIsNone(self.client.get_object(key))

	def test_retries_transient_503(self):
		self.session.fail_next_503 = 2
		key = "sites/acme/private/files/retry.txt"
		self.client.put_object(key, b"ok", content_type="text/plain")
		self.assertEqual(len(self.session.calls), 3)
		self.assertEqual(self.client.get_object(key)[0], b"ok")

	def test_presign_get_url_deterministic_sigv4(self):
		fixed = dt.datetime(2026, 11, 13, 1, 0, 0, tzinfo=dt.timezone.utc)
		url = self.client.presign_get_url(
			"sites/acme/private/files/doc.pdf",
			expires_in=60,
			response_content_disposition="inline; filename=doc.pdf",
			now=fixed,
		)
		self.assertTrue(
			url.startswith(
				"https://credtech-hubtech-files-prod.s3.us-east-1.amazonaws.com/sites/acme/private/files/doc.pdf?"
			)
		)
		self.assertIn("X-Amz-Expires=60", url)
		self.assertIn("X-Amz-Signature=", url)


if __name__ == "__main__":
	unittest.main()
