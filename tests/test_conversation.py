"""Test the native ChatLog adapter without a running Home Assistant instance."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_backend


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_chat_log_is_session_boundary_not_action_authority(self):
        components = ModuleType("homeassistant.components")
        conversation = ModuleType("homeassistant.components.conversation")
        intent = ModuleType("homeassistant.helpers.intent")
        conversation.ConversationEntity = object
        conversation.ConversationEntityFeature = SimpleNamespace(CONTROL=1)
        conversation.AssistantContent = SimpleNamespace
        conversation.ConversationResult = SimpleNamespace
        intent.IntentResponse = Mock()
        with patch.dict(sys.modules, {module.__name__: module for module in (components, conversation, intent)}):
            native = importlib.import_module("bridge_test.conversation")
        bridge = SimpleNamespace(chat=AsyncMock(return_value={"answer": "Eine Lampe wurde ausgeschaltet.", "tools": [{"private": True}]}))
        agent = native.AuralisConversation(bridge)
        agent.entity_id = "conversation.auralis"
        log = SimpleNamespace(conversation_id="ha-session", async_add_assistant_content_without_tools=Mock())
        context = SimpleNamespace(user_id=None)
        request = SimpleNamespace(context=context, conversation_id="requested-id", device_id="device", satellite_id="assist_satellite.office",
                                  language="de", text="Schalte das Licht hier aus")
        result = await agent._async_handle_message(request, log)
        bridge.chat.assert_awaited_once_with(request.text, "de", context, None, memory_key=("ha-session", None, "device", "assist_satellite.office"),
                                           voice_origin=("device", "assist_satellite.office"))
        self.assertEqual(result.conversation_id, "ha-session")
        self.assertEqual(vars(log.async_add_assistant_content_without_tools.call_args.args[0]),
                         {"agent_id": "conversation.auralis", "content": bridge.chat.return_value["answer"]})
