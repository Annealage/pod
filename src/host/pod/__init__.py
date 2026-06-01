"""Annealage Pod host library.

Provides pod discovery (mDNS), registry, and control client built on ampremote.
"""

__version__ = "0.1.0"

from pod.discovery import discover_pods, parse_avahi_line, PodInfo
from pod.registry import load_registry, save_registry, get_pod, set_pod, remove_pod
from pod.client import Pod

__all__ = [
    "__version__",
    "discover_pods",
    "parse_avahi_line",
    "PodInfo",
    "load_registry",
    "save_registry",
    "get_pod",
    "set_pod",
    "remove_pod",
    "Pod",
]
