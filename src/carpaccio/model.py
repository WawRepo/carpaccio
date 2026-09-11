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


# What identifies an entry within each section, for diffing two documents.
_SECTION_KEYS = {
    "wans": "name",
    "vlans": "name",
    "devices": "mac",
    "infrastructure": "mac",
    "firewall_rules": "id",
}


def _entry_label(section: str, entry: dict) -> str:
    """The name a human knows the entry by, falling back to its key."""
    label = entry.get("name") or entry.get("hostname") or ""
    return label or str(entry.get(_SECTION_KEYS[section], "?"))


def _names(section: str, entries: list, limit: int = 3) -> str:
    labels = [_entry_label(section, e) for e in entries[:limit]]
    if len(entries) > limit:
        labels.append(f"+{len(entries) - limit} more")
    return ", ".join(labels)


def describe_diff(old: dict, new: dict) -> list[str]:
    """Human-readable summary of what changed between two documents.

    One line per section that differs, e.g.
    "devices: 2 added (printer, nas); 1 edited (pi4 [ip, notes])".
    Serves both the 409 body (what moved underneath a stale writer) and the
    History tab (what a stored version changed versus its predecessor) - one
    diff function, so the two never drift apart. Each line starts with the
    section's key verbatim, so a caller can route a change to the right tab.

    An empty list means the documents are identical (schema_version aside).
    """
    lines = []
    for section, key in _SECTION_KEYS.items():
        old_list = old.get(section, [])
        new_list = new.get(section, [])
        if old_list == new_list:
            continue
        old_by = {e.get(key): e for e in old_list}
        new_by = {e.get(key): e for e in new_list}
        # Duplicate or missing keys make an entry-level diff a lie
        # (dict-collapse would hide rows), so fall back to plain counts.
        if len(old_by) != len(old_list) or len(new_by) != len(new_list):
            lines.append(f"{section}: {len(old_list)} -> {len(new_list)} entries")
            continue
        added = [new_by[k] for k in new_by if k not in old_by]
        removed = [old_by[k] for k in old_by if k not in new_by]
        edited = [k for k in new_by if k in old_by and new_by[k] != old_by[k]]
        parts = []
        if added:
            parts.append(f"{len(added)} added ({_names(section, added)})")
        if removed:
            parts.append(f"{len(removed)} removed ({_names(section, removed)})")
        if edited:
            if len(edited) <= 3:
                bits = []
                for k in edited:
                    fields = [
                        f
                        for f in sorted(set(new_by[k]) | set(old_by[k]))
                        if new_by[k].get(f) != old_by[k].get(f)
                    ]
                    bits.append(f"{_entry_label(section, new_by[k])} [{', '.join(fields)}]")
                parts.append(f"{len(edited)} edited ({'; '.join(bits)})")
            else:
                parts.append(f"{len(edited)} edited")
        if parts:
            lines.append(f"{section}: " + "; ".join(parts))
    return lines


def summarise_changes(current: dict, submitted: dict) -> list[str]:
    """The 409 body: what differs between the stored document and a rejected
    write, so the loser of a race sees WHAT moved rather than a bare
    'conflict'."""
    return describe_diff(current, submitted) or [
        "stored document differs from the version your edit was based on"
    ]
