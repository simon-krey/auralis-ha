"""Offline tests for panel registration."""
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = pathlib.Path(__file__).parents[1] / "custom_components" / "auralis"


class PanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_url_changes_with_asset(self):
        package = types.ModuleType("panel_test")
        package.__path__ = [str(ROOT)]
        modules = {name: types.ModuleType(name) for name in (
            "homeassistant", "homeassistant.helpers", "homeassistant.helpers.event",
            "homeassistant.components", "homeassistant.components.panel_custom",
            "homeassistant.components.http", "panel_test.backend", "panel_test.websocket",
        )}
        modules["homeassistant.helpers.event"].async_track_time_interval = Mock()
        modules["homeassistant.components.http"].StaticPathConfig = Mock()
        register = modules["homeassistant.components.panel_custom"].async_register_panel = AsyncMock()
        bridge = modules["panel_test.backend"].Bridge = Mock()
        bridge.return_value.load = AsyncMock()
        modules["panel_test.websocket"].register = Mock()
        hass = Mock()
        hass.data = {}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        async def executor(function, *args):
            return function(*args)
        hass.async_add_executor_job = executor
        entry = Mock()
        with patch.dict(sys.modules, {**modules, "panel_test": package}):
            spec = importlib.util.spec_from_file_location("panel_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
            integration = importlib.util.module_from_spec(spec)
            sys.modules["panel_test"] = integration
            spec.loader.exec_module(integration)
            with patch.object(integration.Path, "read_bytes", return_value=b"old panel"):
                await integration.async_setup_entry(hass, entry)
            old_url = register.await_args.kwargs["module_url"]
            with patch.object(integration.Path, "read_bytes", return_value=b"new panel"):
                await integration.async_setup_entry(hass, entry)
            new_url = register.await_args.kwargs["module_url"]
        self.assertTrue(old_url.startswith("/auralis/panel.js?"))
        self.assertNotEqual(old_url, new_url)

    def test_reasoning_selector_is_in_panel(self):
        panel = (ROOT / "panel.js").read_text()
        self.assertIn('id="llm_reasoning"', panel)
        for value in ("default", "on", "off"):
            self.assertIn(f'<option value="{value}">', panel)
