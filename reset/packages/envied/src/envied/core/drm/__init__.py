import base64
from typing import Any, Union
from uuid import UUID

from envied.core.drm.clearkey import ClearKey
from envied.core.drm.monalisa import MonaLisa
from envied.core.drm.playready import PlayReady
from envied.core.drm.widevine import Widevine

DRM_T = Union[ClearKey, Widevine, PlayReady, MonaLisa]


def drm_from_dict(data: dict[str, Any]) -> Union[Widevine, PlayReady]:
    """Reconstruct a Widevine/PlayReady DRM instance from its ``to_dict()`` form.

    Rebuilds the PSSH from the stored base64 and re-injects any saved content keys
    so the resulting object can decrypt without contacting a license server.
    """
    system = data.get("system")
    pssh_b64 = data.get("pssh_b64")
    kids = data.get("kids") or []
    content_keys = data.get("content_keys") or {}

    if not pssh_b64:
        raise ValueError("Cannot reconstruct DRM without a stored PSSH.")

    if system == "PlayReady":
        from pyplayready.system.pssh import PSSH as PlayReadyPSSH

        drm: Union[Widevine, PlayReady] = PlayReady(pssh=PlayReadyPSSH(base64.b64decode(pssh_b64)), pssh_b64=pssh_b64)
    elif system == "Widevine":
        from pywidevine.pssh import PSSH as WidevinePSSH

        drm = Widevine(pssh=WidevinePSSH(pssh_b64), kid=kids[0] if kids else None)
    else:
        raise ValueError(f"Unsupported DRM system for reconstruction: {system!r}")

    for kid_hex, key in content_keys.items():
        drm.content_keys[UUID(hex=kid_hex)] = key

    return drm


__all__ = ("ClearKey", "Widevine", "PlayReady", "MonaLisa", "DRM_T", "drm_from_dict")
