"""Native conversation entity."""
from uuid import uuid4

from homeassistant.components.conversation import ConversationEntity, ConversationEntityFeature, ConversationResult
from homeassistant.helpers import intent
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AuralisConversation(hass.data[DOMAIN])])

class AuralisConversation(ConversationEntity):
    _attr_name = "Auralis"
    _attr_unique_id = "auralis_conversation"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, bridge): self.bridge = bridge
    @property
    def supported_languages(self): return "*"

    async def async_process(self, user_input):
        response = intent.IntentResponse(language=user_input.language)
        user_id = user_input.context.user_id
        user = await self.hass.auth.async_get_user(user_id) if user_id else None
        conversation_id = user_input.conversation_id or uuid4().hex
        device_id, satellite_id = user_input.device_id, user_input.satellite_id
        memory_key = (conversation_id, user_id, device_id, satellite_id) if user_id or device_id or satellite_id else None
        try:
            if user_id and (user is None or not user.is_active):
                raise ValueError("Benutzer nicht verfügbar")
            result = await self.bridge.chat(user_input.text, user_input.language, user_input.context, user, memory_key=memory_key)
            response.async_set_speech(result["answer"])
        except ValueError:
            response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, "Die Anfrage konnte nicht verarbeitet werden.")
        return ConversationResult(response=response, conversation_id=conversation_id)
