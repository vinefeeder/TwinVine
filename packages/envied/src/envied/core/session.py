"""Session utilities for creating HTTP sessions with different backends."""

from __future__ import annotations

import warnings

from curl_cffi.requests import Session as CurlSession

from envied.core.config import config

# Globally suppress curl_cffi HTTPS proxy warnings since some proxy providers
# (like NordVPN) require HTTPS URLs but curl_cffi expects HTTP format
warnings.filterwarnings(
    "ignore", message="Make sure you are using https over https proxy.*", category=RuntimeWarning, module="curl_cffi.*"
)


class Session(CurlSession):
    """curl_cffi Session with warning suppression."""

    def request(self, method, url, **kwargs):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="Make sure you are using https over https proxy.*", category=RuntimeWarning
            )
            return super().request(method, url, **kwargs)


def session(browser: str | None = None, **kwargs) -> Session:
    """
    Create a curl_cffi session that impersonates a browser.

    This is a full replacement for requests.Session with browser impersonation
    and anti-bot capabilities. The session uses curl-impersonate under the hood
    to mimic real browser behavior.

    Args:
        browser: Browser to impersonate (e.g. "chrome124", "firefox", "safari").
                 Uses the configured default from curl_impersonate.browser if not specified.
                 See https://github.com/lexiforest/curl_cffi#sessions for available options.
        **kwargs: Additional arguments passed to CurlSession constructor:
                  - headers: Additional headers (dict)
                  - cookies: Cookie jar or dict
                  - auth: HTTP basic auth tuple (username, password)
                  - proxies: Proxy configuration dict
                  - verify: SSL certificate verification (bool, default True)
                  - timeout: Request timeout in seconds (float or tuple)
                  - allow_redirects: Follow redirects (bool, default True)
                  - max_redirects: Maximum redirect count (int)
                  - cert: Client certificate (str or tuple)

    Returns:
        curl_cffi.requests.Session configured with browser impersonation, common headers,
        and equivalent retry behavior to requests.Session.

    Example:
        from envied.core.session import session

        class MyService(Service):
            @staticmethod
            def get_session():
                return session()  # Uses config default browser
    """
    if browser is None:
        browser = config.curl_impersonate.get("browser", "chrome124")

    session_config = {
        "impersonate": browser,
        "timeout": 30.0,
        "allow_redirects": True,
        "max_redirects": 15,
        "verify": True,
    }

    session_config.update(kwargs)
    session_obj = Session(**session_config)
    session_obj.headers.update(config.headers)

    return session_obj
