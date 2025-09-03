import json
from enum import Enum
from typing import Iterator, Optional, Union
from uuid import UUID

from requests import Session

from envied.core import __version__
from envied.core.vault import Vault


class InsertResult(Enum):
    FAILURE = 0
    SUCCESS = 1
    ALREADY_EXISTS = 2


class HTTP(Vault):
    """Key Vault using HTTP API with support for both query parameters and JSON payloads."""

    def __init__(
        self,
        name: str,
        host: str,
        password: str,
        username: Optional[str] = None,
        api_mode: str = "query",
        no_push: bool = False,
    ):
        """
        Initialize HTTP Vault.

        Args:
            name: Vault name
            host: Host URL
            password: Password for query mode or API token for json mode
            username: Username (required for query mode, ignored for json mode)
            api_mode: "query" for query parameters or "json" for JSON API
            no_push: If True, this vault will not receive pushed keys
        """
        super().__init__(name, no_push)
        self.url = host
        self.password = password
        self.username = username
        self.api_mode = api_mode.lower()
        self.current_title = None
        self.session = Session()
        self.session.headers.update({"User-Agent": f"envied v{__version__}"})
        self.api_session_id = None

        # Validate configuration based on mode
        if self.api_mode == "query" and not self.username:
            raise ValueError("Username is required for query mode")
        elif self.api_mode not in ["query", "json"]:
            raise ValueError("api_mode must be either 'query' or 'json'")

    def request(self, method: str, params: dict = None) -> dict:
        """Make a request to the JSON API vault."""
        if self.api_mode != "json":
            raise ValueError("request method is only available in json mode")

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

        if self.api_mode == "json":
            try:
                params = {
                    "kid": kid,
                    "service": service.lower(),
                }

                response = self.request("GetKey", params)
                if response.get("status") == "not_found":
                    return None
                keys = response.get("keys", [])
                for key_entry in keys:
                    if isinstance(key_entry, str) and ":" in key_entry:
                        entry_kid, entry_key = key_entry.split(":", 1)
                        if entry_kid == kid:
                            return entry_key
                    elif isinstance(key_entry, dict):
                        if key_entry.get("kid") == kid:
                            return key_entry.get("key")
            except Exception as e:
                print(f"Failed to get key ({e.__class__.__name__}: {e})")
                return None
            return None
        else:  # query mode
            response = self.session.get(
                self.url,
                params={"service": service.lower(), "username": self.username, "password": self.password, "kid": kid},
            )

            data = response.json()

            if data.get("status_code") != 200 or not data.get("keys"):
                return None

            return data["keys"][0]["key"]

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        if self.api_mode == "json":
            # JSON API doesn't support getting all keys, so return empty iterator
            # This will cause the copy command to rely on the API's internal duplicate handling
            return iter([])
        else:  # query mode
            response = self.session.get(
                self.url, params={"service": service.lower(), "username": self.username, "password": self.password}
            )

            data = response.json()

            if data.get("status_code") != 200 or not data.get("keys"):
                return

            for key_entry in data["keys"]:
                yield key_entry["kid"], key_entry["key"]

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if isinstance(kid, UUID):
            kid = kid.hex

        title = getattr(self, "current_title", None)

        if self.api_mode == "json":
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
        else:  # query mode
            response = self.session.get(
                self.url,
                params={
                    "service": service.lower(),
                    "username": self.username,
                    "password": self.password,
                    "kid": kid,
                    "key": key,
                    "title": title,
                },
            )

            data = response.json()

            return data.get("status_code") == 200

    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        for kid, key in kid_keys.items():
            if not key or key.count("0") == len(key):
                raise ValueError("You cannot add a NULL Content Key to a Vault.")

        processed_kid_keys = {
            str(kid).replace("-", "") if isinstance(kid, UUID) else kid: key for kid, key in kid_keys.items()
        }

        inserted_count = 0
        title = getattr(self, "current_title", None)

        if self.api_mode == "json":
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
        else:  # query mode
            for kid, key in processed_kid_keys.items():
                response = self.session.get(
                    self.url,
                    params={
                        "service": service.lower(),
                        "username": self.username,
                        "password": self.password,
                        "kid": kid,
                        "key": key,
                        "title": title,
                    },
                )

                data = response.json()

                if data.get("status_code") == 200 and data.get("inserted", True):
                    inserted_count += 1

        return inserted_count

    def get_services(self) -> Iterator[str]:
        if self.api_mode == "json":
            try:
                response = self.request("GetServices")
                services = response.get("services", [])
                for service in services:
                    yield service
            except Exception:
                return iter([])
        else:  # query mode
            response = self.session.get(
                self.url, params={"username": self.username, "password": self.password, "list_services": True}
            )

            data = response.json()

            if data.get("status_code") != 200:
                return

            services = data.get("services", [])
            for service in services:
                yield service

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
        Available in both API modes.
        """
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if isinstance(kid, UUID):
            kid = kid.hex

        if title is None:
            title = getattr(self, "current_title", None)

        if self.api_mode == "json":
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
        else:  # query mode
            response = self.session.get(
                self.url,
                params={
                    "service": service.lower(),
                    "username": self.username,
                    "password": self.password,
                    "kid": kid,
                    "key": key,
                    "title": title,
                },
            )

            try:
                data = response.json()
                if data.get("status_code") == 200:
                    if data.get("inserted", True):
                        return InsertResult.SUCCESS
                    else:
                        return InsertResult.ALREADY_EXISTS
                else:
                    return InsertResult.FAILURE
            except Exception:
                return InsertResult.FAILURE
