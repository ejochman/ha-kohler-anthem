"""Kohler Anthem hardware models and their outlet topology.

The number of outlets, and **which valve each one lives on**, varies by valve model. Setup
detects the split from the API where it can (``topology.py``: the valve's own settings,
else the controller's configuration — both verified live) and asks the user only when
detection fails; either way everything downstream derives from the resulting model.

Getting this wrong is not cosmetic. On a 4-outlet system, outlet 3 is the *first* outlet of
valve 2 — but on a 6-outlet system, outlet 3 is the *third* outlet of valve 1. Code that
assumes "valve1 carries outlets 1-3" silently commands the wrong outlet on a K-28211.

Each valve exposes three outlet bits in its command word regardless of how many are
physically installed; a model simply uses fewer of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ValveModel:
    """One Anthem digital valve model and how its outlets map onto the two valves."""

    sku: str
    name: str
    outlets_valve1: int
    outlets_valve2: int

    @property
    def total_outlets(self) -> int:
        """How many physical outlets this model has."""
        return self.outlets_valve1 + self.outlets_valve2

    @property
    def uses_valve2(self) -> bool:
        """Whether the second valve is populated.

        When it is not, ``secondaryValve1`` must still be present in the payload and is
        sent as the all-zero ignore word.
        """
        return self.outlets_valve2 > 0

    @property
    def zones(self) -> list[int]:
        """The zone numbers this model actually has — ``[1]`` or ``[1, 2]``.

        Zones are the hardware's own unit: a multi-outlet Anthem is physically two valve
        bodies joined, and every API surface addresses them separately. Naming entities per
        zone means no global outlet numbering has to be invented, and therefore no
        model-dependent mapping can be got wrong.
        """
        return [1, 2] if self.uses_valve2 else [1]

    def outlets_in_zone(self, zone: int) -> int:
        """How many outlets that zone has."""
        if zone == 1:
            return self.outlets_valve1
        if zone == 2:
            return self.outlets_valve2
        raise ValueError(f"{self.sku} has zones {self.zones}; got {zone}")

    def outlet_location(self, outlet: int) -> tuple[int, int]:
        """Map a 1-based GLOBAL outlet number to ``(zone, bit_index)``.

        Retained for the few places that genuinely need a flat sequence — chiefly combining
        the controller's two zone arrays. **Entities are named per zone instead**, because
        this mapping is model-dependent (outlet 3 is zone 2's first on a K-28211 but zone
        1's third on a K-28212) and getting it wrong silently commands the wrong outlet.
        """
        if not 1 <= outlet <= self.total_outlets:
            raise ValueError(
                f"{self.sku} has outlets 1-{self.total_outlets}; got {outlet}"
            )
        if outlet <= self.outlets_valve1:
            return 1, outlet - 1
        return 2, outlet - self.outlets_valve1 - 1

    def outlet_id(self, zone: int, outlet: int) -> int:
        """Map a 1-based ``(zone, outlet)`` to the valve's 0-based ``outLetId``.

        Each valve body occupies three ``outLetId`` slots (0-2 on Valve 1, 3-5 on
        Valve 2) regardless of how many outlets are physically populated, so Zone 2's
        first outlet is always ``outLetId`` 3 — even on a 2+2 K-28211 where
        ``outlets_valve1`` is 2.
        """
        count = self.outlets_in_zone(zone)
        if not 1 <= outlet <= count:
            raise ValueError(
                f"{self.sku} zone {zone} has outlets 1-{count}; got {outlet}"
            )
        return (zone - 1) * 3 + (outlet - 1)

    def outlet_from_id(self, outlet_id: int) -> int | None:
        """Map a 0-based hardware ``outLetId`` to a 1-based global outlet, or ``None``.

        Inverts the hardware's fixed 3-slot-per-valve ``outLetId`` numbering (0-2 for
        zone 1, 3-5 for zone 2) into the contiguous ``1..total_outlets`` numbering
        expected by :meth:`outlet_location`. Returns ``None`` if ``outlet_id`` falls
        outside this model's populated outlets.
        """
        if outlet_id < 0:
            return None
        zone = outlet_id // 3 + 1
        bit = outlet_id % 3
        if zone not in self.zones or bit >= self.outlets_in_zone(zone):
            return None
        return (bit + 1) if zone == 1 else (self.outlets_valve1 + bit + 1)

    def split_outlets(self, outlets: list[bool]) -> tuple[list[bool], list[bool]]:
        """Split per-outlet flags into (valve1 flags, valve2 flags)."""
        if len(outlets) != self.total_outlets:
            raise ValueError(
                f"{self.sku} has {self.total_outlets} outlets, got {len(outlets)} flags"
            )
        return (
            outlets[: self.outlets_valve1],
            outlets[self.outlets_valve1 :],
        )


# The four Anthem digital valve models. Valve/outlet splits are from the product line;
# only K-28212 has been exercised against real hardware.
VALVE_MODELS: dict[str, ValveModel] = {
    "K-28209": ValveModel("K-28209", "Anthem 2-outlet valve", 2, 0),
    "K-28210": ValveModel("K-28210", "Anthem 3-outlet valve", 3, 0),
    "K-28211": ValveModel("K-28211", "Anthem 4-outlet valve", 2, 2),
    "K-28212": ValveModel("K-28212", "Anthem 6-outlet valve", 3, 3),
}

DEFAULT_VALVE_MODEL = "K-28212"


def model_for_topology(outlets_valve1: int, outlets_valve2: int) -> ValveModel:
    """Build a model from a detected outlet split, naming it if it matches a known SKU.

    Real installs are not obliged to match Kohler's four catalogue models, so an
    unrecognised split still produces a usable model rather than an error.
    """
    for model in VALVE_MODELS.values():
        if (model.outlets_valve1, model.outlets_valve2) == (
            outlets_valve1,
            outlets_valve2,
        ):
            return model
    label = (
        f"{outlets_valve1}+{outlets_valve2}" if outlets_valve2 else str(outlets_valve1)
    )
    return ValveModel(
        sku="detected",
        name=f"Detected valve ({label} outlets)",
        outlets_valve1=outlets_valve1,
        outlets_valve2=outlets_valve2,
    )


def get_valve_model(sku: str) -> ValveModel:
    """Look up a valve model by SKU, case-insensitively."""
    model = VALVE_MODELS.get(sku.strip().upper())
    if model is None:
        raise ValueError(
            f"Unknown valve model {sku!r}; expected one of {sorted(VALVE_MODELS)}"
        )
    return model


# ---------------------------------------------------------------------------
# App-confirmed, not yet hardware-confirmed
# ---------------------------------------------------------------------------
# On a 2-outlet valve (K-28209, and each half of a K-28211), outlets are assumed to use
# mask bits 0 and 1 — the same low bits a 3-outlet valve uses for its first two outlets.
#
# **Konnect 3.0.6 does exactly this** (2026-10-07, `db0/c.java`): it sorts each valve's
# `outletConfigurations` by `outLetId` and maps list index *i* to bit *i*, so a K-28209 drives
# valve 1 with 0x01/0x02 and a K-28211's zone 2 drives valve 2 with 0x01/0x02. Its setup
# screens number `outLetId` as position−1 except on a 4-port valve, whose zone 2 keeps ids 3
# and 4 — the mapping `outlet_id` implements and PR #2 confirmed on a live K-28211.
#
# Left `False` because the flag means *hardware*-verified, and nobody has yet reported
# toggling each outlet on a 2-outlet valve and watching the right head run. One such report
# flips it. If a 2-outlet valve ever disagrees, `outlet_location` is the single place to
# correct it.
TWO_OUTLET_BIT_MAPPING_VERIFIED = False


# ---------------------------------------------------------------------------
# Where outlet / temperature / flow state comes from
# ---------------------------------------------------------------------------
class OutletStateSource(Enum):
    """Which channel feeds the outlet, temperature, and flow entities."""

    GCS_VALVE_HEX = "gcs_valve_hex"
    """The GCS ``GCS_SOLO_STS`` valve command word. Authoritative and always current."""

    HUB_MQTT = "hub_mqtt"
    """The HUB ``SHOWER_VALVE_STS`` message. The only option on a HUB-only account."""


def resolve_outlet_source(has_gcs: bool, has_hub: bool) -> OutletStateSource | None:
    """Pick the outlet state source for an account.

    **Whenever a GCS device is present, use the GCS valve word — never the HUB's.**

    Partly latency: the HUB trails the valve word — typically ~1 s, measured as much as
    ~5 s stale, and it can skip a short session window entirely (2026-08-21, session 15
    §8e). But mainly reliability: the HUB's coverage of valve-driven sessions is patchy.
    Measured across the 95 GCS water-on episodes where the controller was demonstrably
    alive (2026-08-18 corpus): **51 reported immediately, 12 caught up late, 32 never
    reported at all**. The categorical failure is **presets** — 0 of 15 preset-driven
    openings were ever reported as ON, three publishing ``status: OFF`` while water ran.

    ⚠️ An earlier revision here claimed the HUB "never reports a GCS-driven open outlet on
    any surface", blaming ``solowritesystem`` as such. The 2026-08-18 corpus disproved the
    "never" (51 of 95 seen immediately) and moved the categorical blame to presets; the
    conclusion is unchanged, because "sometimes, late, or never" is exactly what an entity
    source must not be.

    A HUB-only account has no valve word available, so it uses the HUB stream. That works
    because such a system is driven through favorites and the touchscreen, and the outlet
    array does populate for those.

    Returns ``None`` when neither device is present.
    """
    if has_gcs:
        return OutletStateSource.GCS_VALVE_HEX
    if has_hub:
        return OutletStateSource.HUB_MQTT
    return None


# ---------------------------------------------------------------------------
# Touchscreen interfaces — which controller gets a device into the Konnect app
# ---------------------------------------------------------------------------
# This determines what appears on an account, and is not documented by Kohler:
#
#   K-28214       first-generation Anthem touchscreen. Plugs directly into the digital
#                 valve. This is the ONLY way to add a GCS valve to the Konnect app.
#   K-28214-ASC   Anthem Plus touchscreen. Plugs into the HUB system controller, not the
#                 valve. Adds the HUB to the Konnect app — and offers NO option to add the
#                 GCS valve.
#
# So an Anthem Plus owner with only the -ASC screen sees a HUB on their account and no
# GCS, even though a digital valve is physically present.
#
# A digital valve has TWO interface ports, and although the manual does not say so, a
# first-gen K-28214 and a HUB controller can be connected to the SAME valve at once. Both
# interfaces stay consistent because they read state from MQTT and the wired link. That is
# how an account ends up with both a GCS and a HUB entry for one physical shower.
TOUCHSCREEN_GCS = "K-28214"
TOUCHSCREEN_HUB = "K-28214-ASC"
