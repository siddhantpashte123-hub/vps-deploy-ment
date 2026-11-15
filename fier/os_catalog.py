from __future__ import annotations

from dataclasses import dataclass

from .errors import UserError


@dataclass(frozen=True)
class OSChoice:
    id: str
    label: str
    base_image: str
    emoji: str


OS_CHOICES: tuple[OSChoice, ...] = (
    OSChoice("ubuntu-24.04", "Ubuntu 24.04 LTS", "ubuntu:24.04", "🟠"),
    OSChoice("debian-12", "Debian 12 (bookworm)", "debian:12", "🌀"),
)
DEFAULT_OS = "ubuntu-24.04"
_BY_ID = {choice.id: choice for choice in OS_CHOICES}


def valid_os(value: str) -> bool:
    return isinstance(value, str) and value in _BY_ID


def os_choice(value: str) -> OSChoice:
    if not valid_os(value):
        options = ", ".join(choice.id for choice in OS_CHOICES)
        raise UserError(f"Unknown OS `{value}`. Choose one of: {options}.")
    return _BY_ID[value]


def os_label(value: str) -> str:
    choice = _BY_ID.get(value)
    return choice.label if choice else value
