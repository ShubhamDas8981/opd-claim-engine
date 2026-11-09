"""
Put this file at app/core/storage.py

Where uploaded bills are kept.
  STORAGE_BACKEND=local  (default)  -> the uploads/ folder, exactly like before
  STORAGE_BACKEND=s3                -> an Amazon S3 bucket (needs S3_BUCKET, optional AWS_REGION)

The database keeps the same text as before ("uploads/<claim-id>_<file name>"). With S3 that text is simply the S3 key.
On AWS the server uses an IAM role, so there are NO access keys in code or in .env.
"""
import os
import shutil
import tempfile
from contextlib import contextmanager

BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
USE_S3 = BACKEND == "s3"
BUCKET = os.getenv("S3_BUCKET", "").strip()
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
PREFIX = "uploads/"

_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
          ".jpeg": "image/jpeg", ".txt": "text/plain"}
_client = None


def _s3():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client("s3", region_name=REGION,
                               config=Config(signature_version="s3v4", retries={"max_attempts": 3}))
    return _client


def check():
    """Call once at start-up: stop early with a clear message instead of failing on the first upload."""
    if USE_S3 and not BUCKET:
        raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is not set")


def save_upload(claim_id, safe_name, content):
    """Stores the bill and returns the reference that goes into the database."""
    ref = f"{PREFIX}{claim_id}_{safe_name}"
    if USE_S3:
        ext = os.path.splitext(safe_name)[1].lower()
        _s3().put_object(
            Bucket=BUCKET, Key=ref, Body=content,
            ServerSideEncryption="AES256",
            ContentType=_TYPES.get(ext, "application/octet-stream"),
        )
    else:
        os.makedirs("uploads", exist_ok=True)
        with open(ref, "wb") as fh:
            fh.write(content)
    return ref


@contextmanager
def local_copy(ref):
    """Gives a real file on disk that the bill reader can open. For S3 it is a temporary copy that is deleted afterwards."""
    if not USE_S3 or not ref or ref == "no_file":
        yield ref
        return
    tmpdir = tempfile.mkdtemp(prefix="bill_")
    path = os.path.join(tmpdir, os.path.basename(ref))
    try:
        try:
            _s3().download_file(BUCKET, ref, path)
        except Exception as exc:                       # the reader will then report "could not read this bill"
            print(f"❌ S3 download failed for {ref}: {exc}")
            path = os.path.join(tmpdir, "missing")
        yield path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def delete(ref):
    """Removes a withdrawn bill. Never raises."""
    if not ref or ref == "no_file":
        return
    try:
        if USE_S3:
            _s3().delete_object(Bucket=BUCKET, Key=ref)
        else:
            os.remove(ref)
    except Exception:
        pass


def presigned_url(filename, expires=300):
    """A link that opens the bill straight from S3 for 5 minutes. The bucket itself stays private."""
    ext = os.path.splitext(filename)[1].lower()
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": PREFIX + filename,
                "ResponseContentDisposition": "inline",
                "ResponseContentType": _TYPES.get(ext, "application/octet-stream")},
        ExpiresIn=expires,
    )
