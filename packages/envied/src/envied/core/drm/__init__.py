from typing import Union

from envied.core.drm.clearkey import ClearKey
from envied.core.drm.playready import PlayReady
from envied.core.drm.widevine import Widevine

DRM_T = Union[ClearKey, Widevine, PlayReady]


__all__ = ("ClearKey", "Widevine", "PlayReady", "DRM_T")
