import json
from typing import Iterator, Optional, Union
from uuid import UUID
from enum import Enum

from requests import Session

from envied.core import __version__
from envied.core.vault import Vault


class InsertResult(Enum):
    FAILURE = 0
    SUCCESS = 1
    ALREADY_EXISTS = 2


class HTTPAPI(Vault):
    """Key Vault using a structured HTTP API with JSON payloads and token authentication."""

    def __init__(self, name: str, host: str, password: str):
        super().__init__(name)
        self.url = host
        self.password = password  # This is the API token
        self.current_title = None
        self.session = Session()
        self.session.headers.update({"User-Agent": f"Devine v{__version__}"})
        self.api_session_id = None

    def request(self, method: str, params: dict = None) -> dict:
        """Make a request to the HTTPAPI vault."""
        request_payload = {
            "method": method,
            "params": {
                **(params or {}),
                "session_id": self.api_session_id,
            },
            "token": self.password,
        }

        r = self.session.post(self.url, json=request_payload)

        if r.status_code == 404:
            return {"status": "not_found"}

        if not r.ok:
            raise ValueError(f"API returned HTTP Error {r.status_code}: {r.reason.title()}")

        try:
            res = r.json()
        except json.JSONDecodeError:
            if r.status_code == 404:
                return {"status": "not_found"}
            raise ValueError(f"API returned an invalid response: {r.text}")

        if res.get("status_code") != 200:
            raise ValueError(f"API returned an error: {res['status_code']} - {res['message']}")

        if session_id := res.get("message", {}).get("session_id"):
            self.api_session_id = session_id

        return res.get("message", res)

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        if isinstance(kid, UUID):
            kid = kid.hex

        try:
            title = getattr(self, "current_title", None)
            response = self.request(
                "GetKey",
                {
                    "kid": kid,
                    "service": service.lower(),
                    "title": title,
                },
            )
            if response.get("status") == "not_found":
                return None
            keys = response.get("keys", [])
            for key_entry in keys:
                if key_entry["kid"] == kid:
                    return key_entry["key"]
        except Exception as e:
            print(f"Failed to get key ({e.__class__.__name__}: {e})")
            return None

        return None

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        """Get all keys for a service - this may need to be implemented based on your API."""
        try:
            response = self.request(
                "GetKeys",
                {
                    "service": service.lower(),
                },
            )
            keys = response.get("keys", [])
            for key_entry in keys:
                yield key_entry["kid"], key_entry["key"]
        except Exception:
            return iter([])

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if isinstance(kid, UUID):
            kid = kid.hex

        title = getattr(self, "current_title", None)

        try:
            response = self.request(
                "InsertKey",
                {
                    "kid": kid,
                    "key": key,
                    "service": service.lower(),
                    "title": title,
                },
            )
            if response.get("status") == "not_found":
                return False
            return response.get("inserted", False)
        except Exception:
            return False

    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        for kid, key in kid_keys.items():
            if not key or key.count("0") == len(key):
                raise ValueError("You cannot add a NULL Content Key to a Vault.")

        processed_kid_keys = {
            str(kid).replace("-", "") if isinstance(kid, UUID) else kid: key for kid, key in kid_keys.items()
        }

        inserted_count = 0
        title = getattr(self, "current_title", None)

        for kid, key in processed_kid_keys.items():
            try:
                response = self.request(
                    "InsertKey",
                    {
                        "kid": kid,
                        "key": key,
                        "service": service.lower(),
                        "title": title,
                    },
                )
                if response.get("status") == "not_found":
                    continue
                if response.get("inserted", False):
                    inserted_count += 1
            except Exception:
                continue

        return inserted_count

    def get_services(self) -> Iterator[str]:
        """Get available services - this may need to be implemented based on your API."""
        try:
            response = self.request("GetServices")
            services = response.get("services", [])
            for service in services:
                yield service
        except Exception:
            return iter([])

    def set_title(self, title: str):
        """
        Set a title to be used for the next key insertions.
        This is optional and will be sent with add_key requests if available.
        """
        self.current_title = title

    def insert_key_with_result(
        self, service: str, kid: Union[UUID, str], key: str, title: Optional[str] = None
    ) -> InsertResult:
        """
        Insert a key and return detailed result information.
        This method provides more granular feedback than the standard add_key method.
        """
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if isinstance(kid, UUID):
            kid = kid.hex

        if title is None:
            title = getattr(self, "current_title", None)

        try:
            response = self.request(
                "InsertKey",
                {
                    "kid": kid,
                    "key": key,
                    "service": service.lower(),
                    "title": title,
                },
            )

            if response.get("status") == "not_found":
                return InsertResult.FAILURE

            if response.get("inserted", False):
                return InsertResult.SUCCESS
            else:
                return InsertResult.ALREADY_EXISTS

        except Exception:
            return InsertResult.FAILURE
