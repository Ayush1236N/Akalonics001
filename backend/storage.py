"""Object storage abstraction: LocalStorage (dev, disk) and S3Storage (prod, boto3/minio)."""
from __future__ import annotations

import os
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

from config import settings


class StorageError(RuntimeError):
    pass


class Storage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def mtime(self, key: str) -> float: ...

    @abstractmethod
    def healthy(self) -> bool: ...

    # convenience -----------------------------------------------------------
    def put_file(self, key: str, path: str | Path) -> str:
        return self.put(key, Path(path).read_bytes())


class LocalStorage(Storage):
    """Files under STORAGE_DIR. Keys are relative paths; traversal is rejected."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or settings.storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root)):
            raise StorageError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return key

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise StorageError(f"missing object: {key}")
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()

    def list(self, prefix: str = "") -> list[str]:
        base = self.root
        out = []
        for p in base.rglob("*"):
            if p.is_file():
                rel = str(p.relative_to(base))
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def mtime(self, key: str) -> float:
        p = self._path(key)
        return p.stat().st_mtime if p.exists() else 0.0

    def healthy(self) -> bool:
        try:
            probe = self.root / ".healthz"
            probe.write_text(str(time.time()))
            probe.unlink()
            return True
        except Exception:
            return False


class S3Storage(Storage):
    """S3/minio backend. Configured by S3_* env vars."""

    def __init__(self, bucket: str | None = None):
        import boto3  # imported lazily so dev mode needs no boto3 at import time

        self.bucket = bucket or settings.s3_bucket
        if not self.bucket:
            raise StorageError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception:
                pass

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"missing object: {key}") from exc
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            pass

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    def mtime(self, key: str) -> float:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            return head["LastModified"].timestamp()
        except Exception:
            return 0.0

    def healthy(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        if settings.storage_backend.lower() == "s3":
            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage


def set_storage(s: Storage | None) -> None:
    """Test hook."""
    global _storage
    _storage = s
