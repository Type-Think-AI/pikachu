"""Gym badge suites — one module per badge, one pytest marker per badge.

Lane C owns Boulder (types are solid) and Cascade (contracts flow). Other badges'
modules are created by their owning lanes; until then their markers collect nothing,
which the badge runner reports as NOT YET BUILT rather than FAILED.
"""

from __future__ import annotations
