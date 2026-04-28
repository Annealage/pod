# Annealage Pod RP_INFRA-equivalent compatibility shim.
#
# Phase 1: empty class skeleton. Phase 2 fills out the surface defined
# in docs/spec-appendix-B-rp_infra-api.md so that octoprobe /
# testbed_micropython only needs a transport adapter (TCP REPL instead
# of USB serial) to drive the Annealage Pod.


class RpInfra:
    """Stand-in for the existing Octoprobe RP_INFRA API surface.

    Methods mirror the original RP_INFRA module so external test code
    can import this and exercise the Annealage Pod without rewrites. Every
    method raises NotImplementedError until Phase 2.
    """

    def __init__(self):
        pass
