import base64
import secrets
from typing import Optional, Type, Union
from uuid import UUID

import requests
from pywidevine import PSSH, Device, DeviceTypes, Key, RemoteCdm
from pywidevine.license_protocol_pb2 import SignedDrmCertificate, SignedMessage

# Copyright 2024 by DevYukine.


class DecryptLabsRemoteCDM(RemoteCdm):
    def __init__(
        self,
        device_type: Union[DeviceTypes, str],
        system_id: int,
        security_level: int,
        host: str,
        secret: str,
        device_name: str,
        service_name: str,
    ):
        self.response_counter = 0
        self.pssh = None
        self.api_session_ids = {}
        self.license_request = None
        self.service_name = service_name
        self.keys = {}
        try:
            super().__init__(device_type, system_id, security_level, host, secret, device_name)
        except Exception:
            pass
        self.req_session = requests.Session()
        self.req_session.headers.update({"decrypt-labs-api-key": secret})

    @classmethod
    def from_device(cls, device: Device) -> Type["DecryptLabsRemoteCDM"]:
        raise NotImplementedError("You cannot load a DecryptLabsRemoteCDM from a local Device file.")

    def open(self) -> bytes:
        # We stub this method to return a random session ID for now, later we save the api session id and resolve by our random generated one.
        return bytes.fromhex(secrets.token_hex(16))

    def close(self, session_id: bytes) -> None:
        # We stub this method to do nothing.
        pass

    def set_service_certificate(self, session_id: bytes, certificate: Optional[Union[bytes, str]]) -> str:
        if isinstance(certificate, bytes):
            certificate = base64.b64encode(certificate).decode()

        # certificate needs to be base64 to be sent off to the API.
        # it needs to intentionally be kept as base64 encoded SignedMessage.

        self.req_session.signed_device_certificate = certificate
        self.req_session.privacy_mode = True

        return "success"

    def get_service_certificate(self, session_id: bytes) -> Optional[SignedDrmCertificate]:
        raise NotImplementedError("This method is not implemented in this CDM")

    def get_license_challenge(
        self, session_id: bytes, pssh: PSSH, license_type: str = "STREAMING", privacy_mode: bool = True
    ) -> bytes:
        self.pssh = pssh

        res = self.session(
            self.host + "/get-request",
            {
                "init_data": self.pssh.dumps(),
                "service_certificate": self.req_session.signed_device_certificate,
                "scheme": "widevine",
                "service": self.service_name,
            },
        )

        self.license_request = res["challenge"]
        self.api_session_ids[session_id] = res["session_id"]

        return base64.b64decode(self.license_request)

    def parse_license(self, session_id: bytes, license_message: Union[SignedMessage, bytes, str]) -> None:
        session_id_api = self.api_session_ids[session_id]
        if session_id not in self.keys:
            self.keys[session_id] = []
        session_keys = self.keys[session_id]

        if isinstance(license_message, dict) and "keys" in license_message:
            session_keys.extend(
                [
                    Key(kid=Key.kid_to_uuid(x["kid"]), type_=x.get("type", "CONTENT"), key=bytes.fromhex(x["key"]))
                    for x in license_message["keys"]
                ]
            )

        else:
            res = self.session(
                self.host + "/decrypt-response",
                {
                    "session_id": session_id_api,
                    "init_data": self.pssh.dumps(),
                    "license_request": self.license_request,
                    "license_response": license_message,
                    "scheme": "widevine",
                },
            )

            original_keys = res["keys"].replace("\n", " ")
            keys_separated = original_keys.split("--key ")
            formatted_keys = []
            for k in keys_separated:
                if ":" in k:
                    key = k.strip()
                    formatted_keys.append(key)
            for keys in formatted_keys:
                session_keys.append(
                    (
                        Key(
                            kid=UUID(bytes=bytes.fromhex(keys.split(":")[0])),
                            type_="CONTENT",
                            key=bytes.fromhex(keys.split(":")[1]),
                        )
                    )
                )

    def get_keys(self, session_id: bytes, type_: Optional[Union[int, str]] = None) -> list[Key]:
        return self.keys[session_id]

    def session(self, url, data, retries=3):
        res = self.req_session.post(url, json=data).json()

        if res.get("message") != "success":
            if "License Response Decryption Process Failed at the very beginning" in res.get("Error", ""):
                if retries > 0:
                    return self.session(url, data, retries=retries - 1)
                else:
                    raise ValueError(f"CDM API returned an error: {res['Error']}")
            else:
                raise ValueError(f"CDM API returned an error: {res['Error']}")

        return res
