"""Builds a client with its own HTTP session, so Deel cookies never mix with HA's shared jar."""

from __future__ import annotations

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import DeelClient


@callback
def create_client(hass: HomeAssistant) -> tuple[DeelClient, aiohttp.ClientSession]:
    """Return a client and its HTTP session; call ``detach()`` on the session when done.

    HA forbids ``close()`` on sessions it created.
    """
    http = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
    return DeelClient(http), http
