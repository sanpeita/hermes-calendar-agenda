"""Calendar Agenda plugin for Hermes Agent."""

from .tools import register_tools


def register(ctx) -> None:
    register_tools(ctx)
