# pytest fixture: prepend src/mpy/ to sys.path so the frozen annealage_pod
# package is importable on the Unix port without a board build.

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_MPY = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "src", "mpy"))
if _SRC_MPY not in sys.path:
    sys.path.insert(0, _SRC_MPY)
