# netboot.py carries its own copy of the DUT reset GPIO for the fallback park it
# performs when annealage_pod is not yet deployed to the pod filesystem. The pod
# keeps one authoritative pin map, so guard the duplicate against drift.
#
# netboot cannot be imported here (it pulls in network / asyncio.arepl, which
# only exist on the firmware), so the constant is read out of the source.

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_NETBOOT = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "src", "boards", "common", "netboot.py"))


def test_netboot_fallback_nrst_matches_pinmap():
    from annealage_pod import _rp2_pinmap

    with open(_NETBOOT) as f:
        src = f.read()
    m = re.search(r"^_NRST_PIN\s*=\s*const\((\d+)\)", src, re.MULTILINE)
    assert m, "netboot.py no longer defines _NRST_PIN; drop this test or re-point it"
    assert int(m.group(1)) == _rp2_pinmap.NRST, (
        "netboot._NRST_PIN (%s) has drifted from _rp2_pinmap.NRST (%s)"
        % (m.group(1), _rp2_pinmap.NRST))
