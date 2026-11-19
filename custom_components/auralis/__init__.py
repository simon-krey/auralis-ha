"""Auralis integration."""
from pathlib import Path
from datetime import timedelta
from hashlib import sha256
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components import panel_custom
from homeassistant.components.http import StaticPathConfig
from .const import DOMAIN
from .backend import Bridge
from . import websocket

PLATFORMS = ["stt", "conversation"]

async def async_setup(hass, config):
    await hass.http.async_register_static_paths([StaticPathConfig("/auralis/panel.js", str(Path(__file__).parent / "panel.js"), False)])
    websocket.register(hass)
    return True

async def async_setup_entry(hass, entry):
    panel = await hass.async_add_executor_job((Path(__file__).parent / "panel.js").read_bytes)
    panel_url = f"/auralis/panel.js?v={sha256(panel).hexdigest()}"
    bridge = Bridge(hass)
    await bridge.load()
    hass.data[DOMAIN] = bridge
    async def prune_history(_now):
        async with bridge.lock:
            await bridge.prune()
    entry.async_on_unload(async_track_time_interval(hass, prune_history, timedelta(hours=1)))
    await panel_custom.async_register_panel(hass, frontend_url_path="auralis", webcomponent_name="auralis-panel", sidebar_title="Auralis", sidebar_icon="mdi:microphone-message", module_url=panel_url, require_admin=True)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    from homeassistant.components import frontend
    frontend.async_remove_panel(hass, "auralis")
    hass.data.pop(DOMAIN, None)
    return True
