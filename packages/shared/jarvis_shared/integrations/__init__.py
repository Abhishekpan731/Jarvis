"""Integration provider contract. Pure data: no network, no secrets.

A provider is a name, the scopes it asks for, and which agents may use it.
Adding a provider is a new entry here plus a credential in the environment.
Nothing in this module calls a vendor.
"""

from dataclasses import dataclass

PROVIDERS = ("gmail", "google_calendar")

# Read-only. Sending mail and writing calendar events stay behind approval.
_GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
)
_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    scopes: tuple[str, ...]
    agents: tuple[str, ...]


SPECS: dict[str, ProviderSpec] = {
    "gmail": ProviderSpec("gmail", "Gmail", _GMAIL_SCOPES, ("email",)),
    "google_calendar": ProviderSpec(
        "google_calendar", "Google Calendar", _CALENDAR_SCOPES, ("calendar",)
    ),
}


def get_provider(provider_id: str) -> ProviderSpec | None:
    return SPECS.get(provider_id)


def provider_for_agent(agent_id: str) -> str | None:
    """The connection an agent needs, if it needs one."""
    for spec in SPECS.values():
        if agent_id in spec.agents:
            return spec.id
    return None
