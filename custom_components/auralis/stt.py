"""Native STT entity."""
import io
import wave

from homeassistant.components.stt import SpeechToTextEntity, SpeechResult, SpeechResultState, AudioFormats, AudioCodecs, AudioBitRates, AudioSampleRates, AudioChannels
from .const import DOMAIN, MAX_AUDIO

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AuralisSTT(hass.data[DOMAIN])])

class AuralisSTT(SpeechToTextEntity):
    _attr_name = "Auralis"
    _attr_unique_id = "auralis_stt"
    _attr_supported_languages = ["de", "de-DE", "en", "en-US", "en-GB"]
    _attr_supported_formats = [AudioFormats.WAV]
    _attr_supported_codecs = [AudioCodecs.PCM]
    _attr_supported_bit_rates = [AudioBitRates.BITRATE_16]
    _attr_supported_sample_rates = [AudioSampleRates.SAMPLERATE_16000]
    _attr_supported_channels = [AudioChannels.CHANNEL_MONO]

    def __init__(self, bridge): self.bridge = bridge
    @property
    def supported_languages(self): return self._attr_supported_languages
    @property
    def supported_formats(self): return self._attr_supported_formats
    @property
    def supported_codecs(self): return self._attr_supported_codecs
    @property
    def supported_bit_rates(self): return self._attr_supported_bit_rates
    @property
    def supported_sample_rates(self): return self._attr_supported_sample_rates
    @property
    def supported_channels(self): return self._attr_supported_channels

    async def async_process_audio_stream(self, metadata, stream):
        audio = bytearray()
        async for chunk in stream:
            audio.extend(chunk)
            if len(audio) > MAX_AUDIO - 44: return SpeechResult(None, SpeechResultState.ERROR)
        try:
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(audio)
            text = await self.bridge.transcribe(buffer.getvalue(), "audio/wav", metadata.language)
            return SpeechResult(text, SpeechResultState.SUCCESS)
        except ValueError:
            return SpeechResult(None, SpeechResultState.ERROR)
