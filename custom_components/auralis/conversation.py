"""Native conversation entity."""
import logging

from homeassistant.components.conversation import AssistantContent, ConversationEntity, ConversationEntityFeature, ConversationResult
from homeassistant.helpers import intent
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AuralisConversation(hass.data[DOMAIN])])

class AuralisConversation(ConversationEntity):
    _attr_name = "Auralis"
    _attr_unique_id = "auralis_conversation"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, bridge): self.bridge = bridge
    @property
    def supported_languages(self): return "*"

    async def _async_handle_message(self, user_input, chat_log):
        response = intent.IntentResponse(language=user_input.language)
        user_id = user_input.context.user_id
        conversation_id = chat_log.conversation_id
        device_id, satellite_id = user_input.device_id, user_input.satellite_id
        memory_key = (conversation_id, user_id, device_id, satellite_id) if user_id or device_id or satellite_id else None
        english = (user_input.language or "").lower().startswith("en")
        try:
            result = await self.bridge.chat(user_input.text, user_input.language, user_input.context, None, memory_key=memory_key, voice_origin=(device_id, satellite_id))
        except ValueError:
            answer = "I couldn't complete the request." if english else "Ich konnte die Anfrage nicht abschließen."
            response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, answer)
        else:
            answer = result["answer"]
            response.async_set_speech(answer)
        try:
            chat_log.async_add_assistant_content_without_tools(AssistantContent(agent_id=self.entity_id, content=answer))
        except Exception as error:
            _LOGGER.warning("Auralis could not update ChatLog (%s)", type(error).__name__)
        return ConversationResult(response=response, conversation_id=conversation_id)
