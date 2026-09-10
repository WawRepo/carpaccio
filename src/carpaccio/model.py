"""The document model: schema loading, validation, and small pure helpers."""

import json
from importlib import resources

import jsonschema

SCHEMA_VERSION = 1


def load_schema() -> dict:
    ref = resources.files("carpaccio").joinpath("schema/network.schema.json")
    return json.loads(ref.read_text(encoding="utf-8"))


_SCHEMA = load_schema()
_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA)


def empty_document() -> dict:
    """A valid, empty document. Served on first run instead of a stack trace."""
    return {
        "schema_version": SCHEMA_VERSION,
        "wans": [],
        "vlans": [],
        "devices": [],
        "infrastructure": [],
        "firewall_rules": [],
    }


def validation_errors(doc) -> list[str]:
    """Every schema violation as a human-readable line; empty list = valid."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(document root)"
        errors.append(f"{where}: {err.message}")
    return errors


def mac_is_randomised(mac: str) -> bool:
    """True when the locally-administered bit of the first octet is set.

    Computed from the address, never from a vendor list: a randomised MAC
    (iOS/Android/macOS private Wi-Fi address) always sets bit 1 of the first
    octet. Verified against the live UniFi data - it finds exactly the
    phones/tablets/laptops that rotate their address, which decides whether a
    DHCP reservation survives the device rejoining the network.
    """
    return bool(int(mac.split(":")[0], 16) & 2)


def summarise_changes(current: dict, submitted: dict) -> list[str]:
    """What differs between the stored document and a rejected write.

    Returned in a 409 so the loser of a race sees WHAT moved underneath them
    rather than a bare 'conflict'. Section-level granularity is deliberate:
    the UI reloads the whole document anyway, this only has to point a human
    at the right tab.
    """
    changes = []
    for section in ("wans", "vlans", "devices", "infrastructure", "firewall_rules"):
        cur = current.get(section, [])
        sub = submitted.get(section, [])
        if cur != sub:
            changes.append(f"{section}: stored has {len(cur)} entries, your copy has {len(sub)}")
    return changes or ["stored document differs from the version your edit was based on"]
