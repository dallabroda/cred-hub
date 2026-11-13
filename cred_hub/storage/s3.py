from __future__ import annotations

import mimetypes
import os
from typing import Any
from urllib.parse import quote

import frappe
from frappe import _
from werkzeug.wrappers import Response

from cred_hub.storage.s3_client import S3Client, S3Config, S3StorageError

FORCE_DOWNLOAD_EXTENSIONS = frozenset(
	{
		".svg",
		".svgz",
		".html",
		".htm",
		".xhtml",
		".xht",
		".shtml",
		".shtm",
		".mhtml",
		".mht",
		".xml",
		".xsl",
		".xslt",
		".swf",
	}
)


def get_s3_config() -> S3Config | None:
	conf = getattr(frappe.local, "conf", None) or {}
	cfg = conf.get("credhub_s3")
	if isinstance(cfg, dict):
		bucket = (cfg.get("bucket") or "").strip()
		region = (cfg.get("region") or "us-east-1").strip()
		ak = (cfg.get("access_key_id") or "").strip()
		sk = (cfg.get("secret_access_key") or "").strip()
		prefix = (cfg.get("prefix") or "sites").strip().strip("/") or "sites"
		if bucket and ak and sk:
			return S3Config(
				bucket=bucket,
				region=region,
				access_key_id=ak,
				secret_access_key=sk,
				prefix=prefix,
			)

	bucket = (os.environ.get("CREDHUB_S3_BUCKET") or "").strip()
	ak = (os.environ.get("CREDHUB_S3_ACCESS_KEY_ID") or "").strip()
	sk = (os.environ.get("CREDHUB_S3_SECRET_ACCESS_KEY") or "").strip()
	if bucket and ak and sk:
		region = (os.environ.get("CREDHUB_S3_REGION") or "us-east-1").strip()
		prefix = (os.environ.get("CREDHUB_S3_PREFIX") or "sites").strip().strip("/") or "sites"
		return S3Config(
			bucket=bucket,
			region=region,
			access_key_id=ak,
			secret_access_key=sk,
			prefix=prefix,
		)
	return None


def get_s3_client() -> S3Client | None:
	cfg = get_s3_config()
	if not cfg:
		return None
	cached: S3Client | None = getattr(frappe.local, "_credhub_s3_client", None)
	if cached and cached.config == cfg:
		return cached
	client = S3Client(cfg)
	frappe.local._credhub_s3_client = client
	return client


def s3_key_for_url(file_url: str | None, site_name: str | None = None, prefix: str = "sites") -> str | None:
	if not file_url:
		return None
	site = (site_name or getattr(frappe.local, "site", None) or "").strip()
	if not site or ".." in site or "/" in site:
		return None

	rel = file_url.strip()
	site_url = frappe.utils.get_url() if getattr(frappe.local, "site", None) else ""
	if site_url and rel.startswith(site_url) and "/files/" in rel:
		rel = rel.split(site_url, 1)[1]

	rel = rel.lstrip("/")
	if not rel or ".." in rel.split("/"):
		return None

	if rel.startswith("private/files/"):
		subpath = rel
	elif rel.startswith("public/files/"):
		subpath = rel
	elif rel.startswith("files/"):
		subpath = f"public/{rel}"
	else:
		return None

	clean_prefix = (prefix or "sites").strip("/")
	return f"{clean_prefix}/{site}/{subpath}" if clean_prefix else f"{site}/{subpath}"


def _validate_flat_filename(fname: str) -> str:
	clean = (fname or "").strip()
	if not clean or "/" in clean or "\\" in clean or ".." in clean:
		frappe.throw(_("Invalid file name"), title=_("Invalid Upload"))
	return clean


def _register_rollback_delete(client: S3Client, key: str) -> None:
	db = getattr(frappe.local, "db", None)
	if db and hasattr(db, "after_rollback"):
		def _cleanup():
			try:
				client.delete_object(key)
			except Exception:
				pass

		db.after_rollback.add(_cleanup)


def write_file(*args, **kwargs) -> dict[str, str]:
	"""Dual-signature Frappe `write_file` hook supporting both `File.save_file(self)` and
	`file_manager.save_file(fname, content, content_type=..., is_private=...)`."""
	client = get_s3_client()

	# Signature A: File Document instance passed by File.save_file()
	if args and hasattr(args[0], "doctype") and getattr(args[0], "doctype", None) == "File":
		file_doc = args[0]
		if not client:
			return file_doc.save_file_on_filesystem()

		from frappe.core.doctype.file.utils import get_safe_file_name

		raw = file_doc._content
		if isinstance(raw, str):
			raw = raw.encode("utf-8")
			file_doc._content = raw
		file_doc.check_content()

		safe_name = _validate_flat_filename(get_safe_file_name(file_doc.file_name))
		is_priv = bool(int(file_doc.is_private or 0))
		file_doc.file_url = f"/private/files/{safe_name}" if is_priv else f"/files/{safe_name}"

		key = s3_key_for_url(file_doc.file_url, prefix=client.config.prefix)
		if not key:
			frappe.throw(_("Invalid file URL"))

		ctype = getattr(file_doc, "content_type", None) or mimetypes.guess_type(safe_name)[0]
		client.put_object(key, raw, content_type=ctype)
		_register_rollback_delete(client, key)
		return {"file_name": safe_name, "file_url": file_doc.file_url}

	# Signature B: (fname, content, content_type=None, is_private=0) passed by file_manager.save_file()
	fname = args[0] if len(args) > 0 else kwargs["fname"]
	content = args[1] if len(args) > 1 else kwargs["content"]
	content_type = kwargs.get("content_type")
	is_private = bool(int(kwargs.get("is_private") or (args[2] if len(args) > 2 else 0)))

	if not client:
		from frappe.utils.file_manager import save_file_on_filesystem

		return save_file_on_filesystem(fname, content, content_type=content_type, is_private=is_private)

	safe_name = _validate_flat_filename(fname)
	raw_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)
	file_url = f"/private/files/{safe_name}" if is_private else f"/files/{safe_name}"
	key = s3_key_for_url(file_url, prefix=client.config.prefix)
	if not key:
		frappe.throw(_("Invalid file URL"))

	ctype = content_type or mimetypes.guess_type(safe_name)[0]
	client.put_object(key, raw_bytes, content_type=ctype)
	_register_rollback_delete(client, key)
	return {"file_name": safe_name, "file_url": file_url}


def file_exists(file_doc) -> bool | None:
	client = get_s3_client()
	if not client:
		return None
	key = s3_key_for_url(file_doc.file_url, prefix=client.config.prefix)
	if key:
		try:
			if client.head_object(key) is not None:
				return True
		except S3StorageError:
			pass
	try:
		return os.path.exists(file_doc.get_full_path())
	except Exception:
		return False


def read_file_content(file_doc) -> bytes | None:
	client = get_s3_client()
	if not client:
		return None
	key = s3_key_for_url(file_doc.file_url, prefix=client.config.prefix)
	if not key:
		return None
	res = client.get_object(key)
	if res is None:
		# Fallback to local disk for pre-migration files
		return None
	return res[0]


def read_bytes_by_url(file_url: str | None) -> bytes | None:
	"""Helper used by internal readers (e.g. Credit Committee parser) to read bytes from S3 or local disk."""
	if not file_url:
		return None
	client = get_s3_client()
	if client:
		key = s3_key_for_url(file_url, prefix=client.config.prefix)
		if key:
			res = client.get_object(key)
			if res is not None:
				return res[0]

	site_path = frappe.get_site_path()
	rel = file_url.lstrip("/")
	if ".." in rel.split("/"):
		return None
	candidates = []
	if rel.startswith(("private/files/", "public/files/")):
		candidates.append(os.path.join(site_path, rel))
	elif rel.startswith("files/"):
		candidates.append(os.path.join(site_path, "public", rel))
	for candidate in candidates:
		if os.path.isfile(candidate):
			with open(candidate, "rb") as f:
				return f.read()
	return None


def delete_file_data_content(doc, only_thumbnail: bool = False) -> None:
	from frappe.utils.file_manager import delete_file_from_filesystem

	# Always clean any legacy local copy if present
	try:
		delete_file_from_filesystem(doc, only_thumbnail=only_thumbnail)
	except Exception:
		pass

	client = get_s3_client()
	if not client:
		return

	urls = [doc.thumbnail_url] if only_thumbnail else [doc.file_url, doc.thumbnail_url]
	for url in urls:
		if not url:
			continue
		other_refs = frappe.db.count("File", {"file_url": url, "name": ("!=", doc.name)})
		if other_refs > 0:
			continue
		key = s3_key_for_url(url, prefix=client.config.prefix)
		if key:
			try:
				client.delete_object(key)
			except S3StorageError:
				frappe.log_error(title="HubTech S3 Delete Error", message=f"Failed to delete S3 key {key}")


def _stream_s3_response(client: S3Client, key: str, filename: str, is_private: bool) -> Response | None:
	req_method = getattr(getattr(frappe.local, "request", None), "method", "GET")
	ext = os.path.splitext(filename)[1].lower()
	as_attachment = ext in FORCE_DOWNLOAD_EXTENSIONS
	guessed_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

	headers: dict[str, str] = {
		"Cache-Control": "private,max-age=3600,stale-while-revalidate=86400"
		if is_private
		else "public,max-age=86400",
		"X-Content-Type-Options": "nosniff",
	}
	if as_attachment:
		headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
	else:
		headers["Content-Disposition"] = f"inline; filename*=UTF-8''{quote(filename)}"

	if req_method == "HEAD":
		meta = client.head_object(key)
		if meta is None:
			return None
		headers["Content-Type"] = guessed_type
		headers["Content-Length"] = str(meta["content_length"])
		return Response(b"", status=200, headers=headers)

	streamed = client.stream_object(key)
	if streamed is None:
		return None
	upstream_resp, s3_ctype, length = streamed
	headers["Content-Type"] = guessed_type if guessed_type != "application/octet-stream" else s3_ctype
	if length is not None:
		headers["Content-Length"] = str(length)

	def _generate():
		try:
			for chunk in upstream_resp.iter_content(chunk_size=65536):
				if chunk:
					yield chunk
		finally:
			upstream_resp.close()

	return Response(_generate(), status=200, headers=headers, direct_passthrough=True)


def serve_private_file(file_doc, path: str) -> Response | None:
	client = get_s3_client()
	if not client:
		return None
	key = s3_key_for_url(path, prefix=client.config.prefix)
	if not key:
		return None
	filename = file_doc.file_name or os.path.basename(path)
	return _stream_s3_response(client, key, filename=filename, is_private=True)


def serve_public_file(path: str) -> Response | None:
	client = get_s3_client()
	if not client:
		return None
	key = s3_key_for_url(path, prefix=client.config.prefix)
	if not key:
		return None
	filename = os.path.basename(path)
	return _stream_s3_response(client, key, filename=filename, is_private=False)


def migrate_site_files_to_s3(delete_local: bool = True) -> dict[str, Any]:
	"""Idempotently migrates existing site attachments (private/files and public/files) to Amazon S3.
	Verifies remote Content-Length before removing local disk copies."""
	client = get_s3_client()
	if not client:
		return {"enabled": False, "uploaded": 0, "skipped": 0, "removed_local": 0}

	site_path = frappe.get_site_path()
	uploaded = 0
	skipped = 0
	removed_local = 0

	targets = [
		(os.path.join(site_path, "private", "files"), "/private/files"),
		(os.path.join(site_path, "public", "files"), "/files"),
	]
	for folder, url_prefix in targets:
		if not os.path.isdir(folder):
			continue
		for entry in sorted(os.listdir(folder)):
			full_path = os.path.join(folder, entry)
			if not os.path.isfile(full_path) or os.path.islink(full_path):
				continue
			file_url = f"{url_prefix}/{entry}"
			key = s3_key_for_url(file_url, prefix=client.config.prefix)
			if not key:
				continue
			local_size = os.path.getsize(full_path)
			remote = client.head_object(key)
			if remote is not None and remote["content_length"] == local_size:
				skipped += 1
			else:
				with open(full_path, "rb") as f:
					data = f.read()
				ctype = mimetypes.guess_type(entry)[0]
				client.put_object(key, data, content_type=ctype)
				verify = client.head_object(key)
				if not verify or verify["content_length"] != local_size:
					raise S3StorageError(f"S3 size verification failed for {key}")
				uploaded += 1

			if delete_local:
				os.remove(full_path)
				removed_local += 1

	return {
		"enabled": True,
		"site": frappe.local.site,
		"bucket": client.config.bucket,
		"uploaded": uploaded,
		"skipped": skipped,
		"removed_local": removed_local,
	}
