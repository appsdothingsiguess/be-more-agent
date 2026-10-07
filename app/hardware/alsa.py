"""ALSA card discovery and device resolution (no hardware access beyond /proc)."""
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_HEADER = re.compile(r"^\s*(\d+)\s+\[([^\]]*?)\s*\]\s*:\s*(.*)$")
_PLUG = re.compile(r"^(?:plug)?hw:(?:CARD=)?([^,]+)", re.IGNORECASE)


@dataclass
class Card:
    index: int
    id: str
    name: str
    longname: str


@dataclass
class ResolvedDevice:
    device: str
    card: str | None
    source: str  # "explicit" | "match" | "fallback"


def parse_cards(text: str) -> list[Card]:
    cards: list[Card] = []
    current: Card | None = None
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            desc = m.group(3)
            name = desc.split(" - ", 1)[1].strip() if " - " in desc else desc.strip()
            current = Card(int(m.group(1)), m.group(2).strip(), name, "")
            cards.append(current)
        elif current is not None and line.strip():
            current.longname = (current.longname + " " + line.strip()).strip()
    return cards


def read_cards(path: str = "/proc/asound/cards") -> list[Card]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_cards(f.read())
    except OSError:
        return []


def card_from_device(device: str) -> str | None:
    m = _PLUG.match(device.strip())
    return m.group(1) if m else None


def resolve_device(device: str, match: str, fallback: str,
                   cards: list[Card] | None = None) -> ResolvedDevice:
    if device != "auto":
        return ResolvedDevice(device, card_from_device(device), "explicit")
    if cards is None:
        cards = read_cards()
    needle = match.lower()
    for c in cards:
        if needle in c.name.lower() or needle in c.longname.lower():
            return ResolvedDevice(f"plughw:CARD={c.id},DEV=0", c.id, "match")
    log.warning("No ALSA card matching %r; falling back to %s", match, fallback)
    return ResolvedDevice(fallback, card_from_device(fallback), "fallback")
