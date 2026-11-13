from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests


class S3StorageError(RuntimeError):
	"""Raised when Amazon S3 refuses or fails an object operation."""


def _sign(key: bytes, msg: str) -> bytes:
	return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str = "s3") -> bytes:
	k_date = _sign(("AWS4" + secret_key).encode("utf-8"), datestamp)
	k_region = _sign(k_date, region)
	k_service = _sign(k_region, service)
	return _sign(k_service, "aws4_request")


def encode_s3_uri_path(key: str) -> str:
	clean = key.lstrip("/")
	if not clean or ".." in clean.split("/"):
		raise S3StorageError(f"Invalid S3 object key: {key!r}")
	segments = [urllib.parse.quote(seg, safe="-_.~") for seg in clean.split("/") if seg]
	if not segments:
		raise S3StorageError(f"Invalid S3 object key: {key!r}")
	return "/" + "/".join(segments)


@dataclass(frozen=True)
class S3Config:
	bucket: str
	region: str
	access_key_id: str
	secret_access_key: str
	prefix: str = "sites"

	@property
	def host(self) -> str:
		if self.region == "us-east-1":
			return f"{self.bucket}.s3.us-east-1.amazonaws.com"
		return f"{self.bucket}.s3.{self.region}.amazonaws.com"


class S3Client:
	"""Minimal, zero-SDK AWS Signature Version 4 client for Amazon S3 over requests."""

	RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

	def __init__(self, config: S3Config, session: Any | None = None, timeout: float = 30.0):
		self.config = config
		self.session = session or requests.Session()
		self.timeout = timeout

	def _build_signed_headers(
		self,
		method: str,
		uri_path: str,
		query_params: dict[str, str] | None = None,
		payload_hash: str = "UNSIGNED-PAYLOAD",
		extra_headers: dict[str, str] | None = None,
		now: dt.datetime | None = None,
	) -> tuple[str, dict[str, str]]:
		t = now or dt.datetime.now(dt.timezone.utc)
		amz_date = t.strftime("%Y%m%dT%H%M%SZ")
		datestamp = t.strftime("%Y%m%d")

		headers_to_sign: dict[str, str] = {
			"host": self.config.host,
			"x-amz-content-sha256": payload_hash,
			"x-amz-date": amz_date,
		}
		if extra_headers:
			for k, v in extra_headers.items():
				headers_to_sign[k.lower().strip()] = str(v).strip()

		sorted_keys = sorted(headers_to_sign.keys())
		canonical_headers = "".join(f"{k}:{headers_to_sign[k]}\n" for k in sorted_keys)
		signed_headers = ";".join(sorted_keys)

		qp = query_params or {}
		canonical_qs = "&".join(
			f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(qp[k]), safe='-_.~')}"
			for k in sorted(qp.keys())
		)

		canonical_request = "\n".join(
			[method.upper(), uri_path, canonical_qs, canonical_headers, signed_headers, payload_hash]
		)
		scope = f"{datestamp}/{self.config.region}/s3/aws4_request"
		string_to_sign = "\n".join(
			[
				"AWS4-HMAC-SHA256",
				amz_date,
				scope,
				hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
			]
		)
		sig = hmac.new(
			_signing_key(self.config.secret_access_key, datestamp, self.config.region),
			string_to_sign.encode("utf-8"),
			hashlib.sha256,
		).hexdigest()

		req_headers = {k: v for k, v in headers_to_sign.items() if k != "host"}
		req_headers["Authorization"] = (
			f"AWS4-HMAC-SHA256 Credential={self.config.access_key_id}/{scope}, "
			f"SignedHeaders={signed_headers}, Signature={sig}"
		)
		url = f"https://{self.config.host}{uri_path}"
		if canonical_qs:
			url = f"{url}?{canonical_qs}"
		return url, req_headers

	def _request(
		self,
		method: str,
		key: str,
		*,
		body: bytes = b"",
		extra_headers: dict[str, str] | None = None,
		stream: bool = False,
	):
		uri_path = encode_s3_uri_path(key)
		payload_hash = hashlib.sha256(body).hexdigest()
		last_exc: Exception | None = None
		for attempt in range(3):
			url, headers = self._build_signed_headers(
				method,
				uri_path,
				payload_hash=payload_hash,
				extra_headers=extra_headers,
			)
			try:
				resp = self.session.request(
					method=method.upper(),
					url=url,
					data=body if method.upper() in ("PUT", "POST") else None,
					headers=headers,
					timeout=self.timeout,
					stream=stream,
				)
			except Exception as exc:
				last_exc = exc
				if attempt < 2:
					time.sleep(0.3 * (attempt + 1))
					continue
				raise S3StorageError(f"S3 network error on {method} {uri_path}: {type(exc).__name__}") from exc

			if resp.status_code in self.RETRYABLE_STATUSES and attempt < 2:
				resp.close()
				time.sleep(0.3 * (attempt + 1))
				continue
			return resp

		raise S3StorageError(f"S3 request failed after retries: {last_exc}")

	def put_object(self, key: str, content: bytes, content_type: str | None = None) -> dict[str, Any]:
		if isinstance(content, str):
			content = content.encode("utf-8")
		extra = {
			"Content-Type": content_type or "application/octet-stream",
			"x-amz-server-side-encryption": "AES256",
		}
		resp = self._request("PUT", key, body=content, extra_headers=extra)
		if resp.status_code not in (200, 201):
			raise S3StorageError(f"S3 PUT failed with HTTP {resp.status_code}")
		return {"key": key.lstrip("/"), "size": len(content), "etag": resp.headers.get("ETag", "")}

	def head_object(self, key: str) -> dict[str, Any] | None:
		resp = self._request("HEAD", key)
		if resp.status_code == 404:
			return None
		if resp.status_code != 200:
			raise S3StorageError(f"S3 HEAD failed with HTTP {resp.status_code}")
		return {
			"content_length": int(resp.headers.get("Content-Length") or 0),
			"content_type": resp.headers.get("Content-Type") or "application/octet-stream",
			"etag": resp.headers.get("ETag", ""),
		}

	def get_object(self, key: str) -> tuple[bytes, str] | None:
		resp = self._request("GET", key)
		if resp.status_code == 404:
			return None
		if resp.status_code != 200:
			raise S3StorageError(f"S3 GET failed with HTTP {resp.status_code}")
		return resp.content, resp.headers.get("Content-Type") or "application/octet-stream"

	def stream_object(self, key: str) -> tuple[Any, str, int | None] | None:
		resp = self._request("GET", key, stream=True)
		if resp.status_code == 404:
			resp.close()
			return None
		if resp.status_code != 200:
			resp.close()
			raise S3StorageError(f"S3 stream GET failed with HTTP {resp.status_code}")
		length_hdr = resp.headers.get("Content-Length")
		length = int(length_hdr) if length_hdr and length_hdr.isdigit() else None
		ctype = resp.headers.get("Content-Type") or "application/octet-stream"
		return resp, ctype, length

	def delete_object(self, key: str) -> bool:
		resp = self._request("DELETE", key)
		if resp.status_code in (200, 204, 404):
			return True
		raise S3StorageError(f"S3 DELETE failed with HTTP {resp.status_code}")

	def presign_get_url(
		self,
		key: str,
		expires_in: int = 60,
		response_content_disposition: str | None = None,
		response_content_type: str | None = None,
		now: dt.datetime | None = None,
	) -> str:
		uri_path = encode_s3_uri_path(key)
		t = now or dt.datetime.now(dt.timezone.utc)
		amz_date = t.strftime("%Y%m%dT%H%M%SZ")
		datestamp = t.strftime("%Y%m%d")
		scope = f"{datestamp}/{self.config.region}/s3/aws4_request"

		params: dict[str, str] = {
			"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
			"X-Amz-Credential": f"{self.config.access_key_id}/{scope}",
			"X-Amz-Date": amz_date,
			"X-Amz-Expires": str(max(1, min(int(expires_in), 3600))),
			"X-Amz-SignedHeaders": "host",
		}
		if response_content_disposition:
			params["response-content-disposition"] = response_content_disposition
		if response_content_type:
			params["response-content-type"] = response_content_type

		canonical_qs = "&".join(
			f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
			for k, v in sorted(params.items())
		)
		canonical_request = "\n".join(
			["GET", uri_path, canonical_qs, f"host:{self.config.host}\n", "host", "UNSIGNED-PAYLOAD"]
		)
		string_to_sign = "\n".join(
			[
				"AWS4-HMAC-SHA256",
				amz_date,
				scope,
				hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
			]
		)
		sig = hmac.new(
			_signing_key(self.config.secret_access_key, datestamp, self.config.region),
			string_to_sign.encode("utf-8"),
			hashlib.sha256,
		).hexdigest()
		return f"https://{self.config.host}{uri_path}?{canonical_qs}&X-Amz-Signature={sig}"
