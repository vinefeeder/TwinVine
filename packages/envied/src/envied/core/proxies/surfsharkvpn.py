import json
import random
import re
from typing import Optional

import requests

from envied.core.proxies.proxy import Proxy


class SurfsharkVPN(Proxy):
    def __init__(self, username: str, password: str, server_map: Optional[dict[str, int]] = None):
        """
        Proxy Service using SurfsharkVPN Service Credentials.

        A username and password must be provided. These are Service Credentials, not your Login Credentials.
        The Service Credentials can be found here: https://my.surfshark.com/vpn/manual-setup/main/openvpn
        """
        if not username:
            raise ValueError("No Username was provided to the SurfsharkVPN Proxy Service.")
        if not password:
            raise ValueError("No Password was provided to the SurfsharkVPN Proxy Service.")
        if not re.match(r"^[a-z0-9]{48}$", username + password, re.IGNORECASE) or "@" in username:
            raise ValueError(
                "The Username and Password must be SurfsharkVPN Service Credentials, not your Login Credentials. "
                "The Service Credentials can be found here: https://my.surfshark.com/vpn/manual-setup/main/openvpn"
            )

        if server_map is not None and not isinstance(server_map, dict):
            raise TypeError(f"Expected server_map to be a dict mapping a region to a server ID, not '{server_map!r}'.")

        self.username = username
        self.password = password
        self.server_map = server_map or {}

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(set(x.get("country") for x in self.countries if x.get("country")))
        servers = sum(1 for x in self.countries if x.get("connectionName"))

        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP(SSL) proxy URI for a SurfsharkVPN server.
        """
        query = query.lower()
        if re.match(r"^[a-z]{2}\d+$", query):
            # country and surfsharkvpn server id, e.g., au-per, be-anr, us-bos
            hostname = f"{query}.prod.surfshark.com"
        else:
            if query.isdigit():
                # country id
                country = self.get_country(by_id=int(query))
            elif re.match(r"^[a-z]+$", query):
                # country code
                country = self.get_country(by_code=query)
            else:
                raise ValueError(f"The query provided is unsupported and unrecognized: {query}")
            if not country:
                # SurfsharkVPN doesnt have servers in this region
                return

            server_mapping = self.server_map.get(country["countryCode"].lower())
            if server_mapping:
                # country was set to a specific server ID in config
                hostname = f"{country['code'].lower()}{server_mapping}.prod.surfshark.com"
            else:
                # get the random server ID
                random_server = self.get_random_server(country["countryCode"])
                if not random_server:
                    raise ValueError(
                        f"The SurfsharkVPN Country {query} currently has no random servers. "
                        "Try again later. If the issue persists, double-check the query."
                    )
                hostname = random_server

        return f"https://{self.username}:{self.password}@{hostname}:443"

    def get_country(self, by_id: Optional[int] = None, by_code: Optional[str] = None) -> Optional[dict]:
        """Search for a Country and it's metadata."""
        if all(x is None for x in (by_id, by_code)):
            raise ValueError("At least one search query must be made.")

        for country in self.countries:
            if all(
                [
                    by_id is None or country["id"] == int(by_id),
                    by_code is None or country["countryCode"] == by_code.upper(),
                ]
            ):
                return country

    def get_random_server(self, country_id: str):
        """
        Get the list of random Server for a Country.

        Note: There may not always be more than one recommended server.
        """
        country = [x["connectionName"] for x in self.countries if x["countryCode"].lower() == country_id.lower()]
        try:
            country = random.choice(country)
            return country
        except Exception:
            raise ValueError("Could not get random countrycode from the countries list.")

    @staticmethod
    def get_countries() -> list[dict]:
        """Get a list of available Countries and their metadata."""
        res = requests.get(
            url="https://api.surfshark.com/v3/server/clusters/all",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Content-Type": "application/json",
            },
        )
        if not res.ok:
            raise ValueError(f"Failed to get a list of SurfsharkVPN countries [{res.status_code}]")

        try:
            return res.json()
        except json.JSONDecodeError:
            raise ValueError("Could not decode list of SurfsharkVPN countries, not JSON data.")
