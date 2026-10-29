"""Handler package: assembles routers and builds the Persian command registry."""

from __future__ import annotations

from aiogram import Router

from .registry import registry


def build_routers() -> list[Router]:
    """Import every handler module, build the command index and return routers.

    Router order matters:

    1. ``commands`` - Persian command dispatcher (falls through when no command)
    2. ``panel``    - callback queries (menu navigation and settings)
    3. ``members``  - join / leave / service messages
    4. ``messages`` - the general group message pipeline
    5. ``errors``   - last resort error handler
    """
    from . import (  # noqa: F401 - import order registers commands
        approval,
        commands,
        entertainment_market,
        errors,
        federation_owner,
        filters_notes,
        locks,
        members,
        mention_all,
        multi_ban,
        messages,
        moderation,
        panel,
        stats,
        tools,
        welcome_settings,
    )
    from .welcome_settings import _register_service_toggles

    _register_service_toggles()
    registry.build()

    return [
        commands.router,
        approval.router,
        panel.router,
        multi_ban.router,
        members.router,
        messages.router,
        errors.router,
    ]


__all__ = ["build_routers", "registry"]
