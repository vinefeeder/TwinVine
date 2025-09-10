from __future__ import annotations

import threading
import zlib
from datetime import datetime, timedelta
from os import stat_result
from pathlib import Path
from typing import Any, Optional, Union
import jsonpickle
import jwt
from envied.core.config import config

EXP_T = Union[datetime, str, int, float]


class Cacher:
    """
    Cacher for Services to get and set arbitrary data with expiration dates.

    Multiton: one instance per (service_tag, key, version) to avoid duplicate objects
    pointing at the same cache file.
    """

    # --- Multiton registry ---
    _instances: dict[tuple[str, Optional[str], Optional[int]], "Cacher"] = {}
    _lock = threading.RLock()

    def __new__(
        cls,
        service_tag: str,
        key: Optional[str] = None,
        version: Optional[int] = 1,
        data: Optional[Any] = None,
        expiration: Optional[datetime] = None,
    ):
        ident = (service_tag, key, version)
        with cls._lock:
            inst = cls._instances.get(ident)
            if inst is None:
                inst = super().__new__(cls)
                cls._instances[ident] = inst
        return inst

    def __init__(
        self,
        service_tag: str,
        key: Optional[str] = None,
        version: Optional[int] = 1,
        data: Optional[Any] = None,
        expiration: Optional[datetime] = None,
    ) -> None:
        # Make __init__ idempotent for Multiton
        if getattr(self, "_initialized", False):
            return

        self.service_tag = service_tag
        self.key = key
        self.version = version
        self.data = data or {}
        self.expiration = expiration

        self._initialized = True  # mark as initialized

        if self.expiration and self.expired:
            # if it's expired, remove the data for safety and delete cache file
            self.data = None
            # Guard: key might be None; only unlink if there is a concrete file path
            try:
                self.path.unlink()
            except Exception:
                pass

    def __bool__(self) -> bool:
        return bool(self.data)

    @property
    def path(self) -> Path:
        """Get the path at which the cache will be read and written."""
        # Guard against None key (e.g., before 'get' is called)
        if self.key is None:
            # Create a directory path to the service cache area (no file yet)
            return (config.directories.cache / self.service_tag / "__unbound__").with_suffix(".json")
        return (config.directories.cache / self.service_tag / self.key).with_suffix(".json")

    @property
    def expired(self) -> bool:
        return bool(self.expiration and self.expiration < datetime.now())

    def get(self, key: str, version: int = 1) -> "Cacher":
        """
        Get Cached data for the Service by Key.
        :param key: the filename to save the data to, should be url-safe.
        :param version: the config data version you expect to use.
        :returns: Cache object containing the cached data or empty if the file does not exist.
        """
        # Use the Multiton constructor; this will reuse an existing instance
        # for (service_tag, key, version) if created before.
        cache = type(self)(self.service_tag, key, version)

        if cache.path.is_file():
            data = jsonpickle.loads(cache.path.read_text(encoding="utf8"))
            payload = data.copy()
            del payload["crc32"]
            checksum = data["crc32"]
            calculated = zlib.crc32(jsonpickle.dumps(payload).encode("utf8"))
            if calculated != checksum:
                raise ValueError(
                    f"The checksum of the Cache payload mismatched. "
                    f"Checksum: {checksum} !== Calculated: {calculated}"
                )
            cache.data = data["data"]
            cache.expiration = data["expiration"]
            cache.version = data["version"]
            if cache.version != version:
                raise ValueError(
                    f"The version of your {self.service_tag} {key} cache is outdated. "
                    f"Please delete: {cache.path}"
                )
        else:
            # Ensure empty state if file absent
            cache.data = {}
            cache.expiration = None
            cache.version = version

        return cache

    def set(self, data: Any, expiration: Optional[EXP_T] = None) -> Any:
        """
        Set Cached data for the Service by Key.
        :param data: absolutely anything including None.
        :param expiration: when the data expires, optional. Can be ISO 8601, seconds
            til expiration, unix timestamp, or a datetime object.
        :returns: the data provided for quick wrapping of functions or vars.
        """
        self.data = data

        if not expiration:
            try:
                expiration = jwt.decode(self.data, options={"verify_signature": False})["exp"]
            except jwt.DecodeError:
                pass
            except Exception:
                # data may not be a JWT-encoded object; ignore
                pass

        self.expiration = self._resolve_datetime(expiration) if expiration else None

        payload = {"data": self.data, "expiration": self.expiration, "version": self.version}
        payload["crc32"] = zlib.crc32(jsonpickle.dumps(payload).encode("utf8"))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(jsonpickle.dumps(payload))

        return self.data

    def stat(self) -> stat_result:
        """
        Get Cache file OS Stat data like Creation Time, Modified Time, and such.
        :returns: an os.stat_result tuple
        """
        return self.path.stat()

    @staticmethod
    def _resolve_datetime(timestamp: EXP_T) -> datetime:
        """
        Resolve multiple formats of a Datetime or Timestamp to an absolute Datetime.
        """
        if isinstance(timestamp, datetime):
            return timestamp
        if isinstance(timestamp, str):
            if timestamp.endswith("Z"):
                # fromisoformat doesn't accept the final Z
                timestamp = timestamp.split("Z")[0]
            try:
                return datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = float(timestamp)
        try:
            if len(str(int(timestamp))) == 13:  # JS-style timestamp
                timestamp /= 1000
            timestamp = datetime.fromtimestamp(timestamp)
        except ValueError:
            raise ValueError(f"Unrecognized Timestamp value {timestamp!r}")
        if timestamp < datetime.now():
            # Likely an amount of seconds until expiration
            timestamp = timestamp + timedelta(seconds=datetime.now().timestamp())
        return timestamp
