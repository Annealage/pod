"""Shared bootstrap for the prototypes/validation hardware scripts.

Throwaway validation harness (prototypes/ per repo conventions), run by the
orchestrator against the live pod after the firmware is deployed. Puts the host
`pod` package on sys.path (the package is at src/host) and resolves a Pod client
from the registry by label, so each validation script is a thin main() over the
real client API.

Not built into firmware. No hardware is touched by importing this module; the
Pod client is only constructed, and a connect happens on the first exec.
"""

import os
import sys

# src/host holds the `pod` package; this file is at prototypes/validation.
_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST = os.path.normpath(os.path.join(_HERE, "..", "..", "src", "host"))
if _HOST not in sys.path:
    sys.path.insert(0, _HOST)

from pod import registry              # noqa: E402
from pod.client import Pod            # noqa: E402

DEFAULT_LABEL = "annealage-pod"


def pod_from_label(label=None):
    """Build a Pod client from the registry entry for `label`.

    Falls back to DEFAULT_LABEL, then argv[1] if given. Raises a clear error if
    the label is not registered (run `pod list` / discovery first).
    """
    label = label or (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LABEL)
    entry = registry.get_pod(label)
    if entry is None:
        raise SystemExit(
            "pod label %r is not in the registry (~/.config/pod/pods.json); "
            "run discovery/registration first" % label)
    return label, Pod.from_entry(entry)
