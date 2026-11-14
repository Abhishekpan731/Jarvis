"""The provider-independent command contract.

Every interaction surface — web, a future Siri app, a future Google Assistant
app, desktop, or a direct API call — submits a command in this shape. Siri and
Google Assistant are interaction sources, not reasoning providers, so they are
values of `source` and nothing more.

Normalization is pure and side-effect free so it can be tested without a
database or a model.
"""

SOURCES = ("siri", "google_assistant", "web", "desktop", "api")
INPUT_TYPES = ("voice", "text")

# What a voice surface may say. Kept short on purpose: a spoken reply that runs
# long is ignored.
_VOICE_LIMIT = 240


def normalize_command(raw: str) -> str:
    """Collapse whitespace and strip surrounding quotes a voice surface adds."""
    text = " ".join(raw.split())
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    return text


def voice_reply(text: str) -> str:
    """Shorten a reply for speech. The full text stays available separately."""
    flat = " ".join(text.split())
    if len(flat) <= _VOICE_LIMIT:
        return flat
    cut = flat[:_VOICE_LIMIT].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "."
