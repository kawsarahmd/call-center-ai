from enum import Enum
from helpers.config import CONFIG
from helpers.logging import build_logger
from models.call import CallModel
from models.message import StyleEnum as MessageStyleEnum
from typing import Generator, Optional
from azure.communication.callautomation import (
    FileSource,
    PhoneNumberIdentifier,
    RecognitionChoice,
    RecognizeInputType,
    SsmlSource,
    CallConnectionClient,
)
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
from models.message import (
    MessageModel,
    PersonaEnum as MessagePersonaEnum,
    StyleEnum as MessageStyleEnum,
)
import re


_logger = build_logger(__name__)
_SENTENCE_PUNCTUATION_R = r"(\. |\.$|[!?;])"


class ContextEnum(str, Enum):
    CONNECT_AGENT = "connect_agent"
    GOODBYE = "goodbye"
    TRANSFER_FAILED = "transfer_failed"


def tts_sentence_split(text: str, include_last: bool) -> Generator[str, None, None]:
    """
    Split a text into sentences.
    """
    # Split by sentence by punctuation
    splits = re.split(_SENTENCE_PUNCTUATION_R, text)
    for i, split in enumerate(splits):
        if i % 2 == 1:  # Skip punctuation
            continue
        if not split:  # Skip empty lines
            continue
        if i == len(splits) - 1:  # Skip last line in case of missing punctuation
            if include_last:
                yield split
        else:  # Add punctuation back
            yield split + splits[i + 1]


# TODO: Disable or lower profanity filter. The filter seems enabled by default, it replaces words like "holes in my roof" by "*** in my roof". This is not acceptable for a call center.
async def _handle_recognize_media(
    client: CallConnectionClient,
    call: CallModel,
    sound_url: str,
) -> None:
    """
    Play a media to a call participant and start recognizing the response.
    """
    _logger.debug(f"Recognizing media")
    try:
        client.start_recognizing_media(
            end_silence_timeout=3,  # Sometimes user includes breaks in their speech
            input_type=RecognizeInputType.SPEECH,
            play_prompt=FileSource(url=sound_url),
            speech_language=call.lang.short_code,
            target_participant=PhoneNumberIdentifier(call.phone_number),  # type: ignore
        )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before recognizing")
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            _logger.debug(f"Call hung up before playing")
        else:
            raise e


async def handle_media(
    client: CallConnectionClient,
    call: CallModel,
    sound_url: str,
    context: Optional[str] = None,
) -> None:
    try:
        client.play_media(
            operation_context=context,
            play_source=FileSource(url=sound_url),
        )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before playing")
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            _logger.debug(f"Call hung up before playing")
        else:
            raise e


async def handle_recognize_text(
    client: CallConnectionClient,
    call: CallModel,
    style: MessageStyleEnum = MessageStyleEnum.NONE,
    text: Optional[str] = None,
    store: bool = True,
) -> None:
    """
    Play a text to a call participant and start recognizing the response.

    If `store` is `True`, the text will be stored in the call messages. Starts by playing text, then the "ready" sound, and finally starts recognizing the response.
    """
    if text:
        await handle_play(
            call=call,
            client=client,
            store=store,
            style=style,
            text=text,
        )

    await _handle_recognize_media(
        call=call,
        client=client,
        sound_url=CONFIG.prompts.sounds.ready(),
    )


async def handle_play(
    client: CallConnectionClient,
    call: CallModel,
    text: str,
    style: MessageStyleEnum = MessageStyleEnum.NONE,
    context: Optional[str] = None,
    store: bool = True,
) -> None:
    """
    Play a text to a call participant.

    If store is True, the text will be stored in the call messages. Compatible with text larger than 400 characters, in that case the text will be split in chunks and played sequentially.

    See: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts
    """
    if store:
        if (
            call.messages and call.messages[-1].persona == MessagePersonaEnum.ASSISTANT
        ):  # If the last message was from the assistant, append to it
            call.messages[-1].content += f" {text}"
        else:  # Otherwise, create a new message
            call.messages.append(
                MessageModel(
                    content=text,
                    persona=MessagePersonaEnum.ASSISTANT,
                    style=style,
                )
            )

    # Split text in chunks of max 400 characters, separated by sentence
    chunks = []
    chunk = ""
    for to_add in tts_sentence_split(text, True):
        if len(chunk) + len(to_add) >= 400:
            chunks.append(chunk.strip())  # Remove trailing space
            chunk = ""
        chunk += to_add
    if chunk:
        chunks.append(chunk)

    try:
        for chunk in chunks:
            _logger.info(f"Playing text: {text} ({style})")
            client.play_media(
                operation_context=context,
                play_source=_audio_from_text(chunk, style, call),
            )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before playing")
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            _logger.debug(f"Call hung up before playing")
        else:
            raise e


def _audio_from_text(text: str, style: MessageStyleEnum, call: CallModel) -> SsmlSource:
    """
    Generate an audio source that can be read by Azure Communication Services SDK.

    Text requires to be SVG escaped, and SSML tags are used to control the voice. Plus, text is slowed down by 5% to make it more understandable for elderly people. Text is also truncated to 400 characters, as this is the limit of Azure Communication Services TTS, but a warning is logged.
    """
    # Azure Speech Service TTS limit is 400 characters
    if len(text) > 400:
        _logger.warning(
            f"Text is too long to be processed by TTS, truncating to 400 characters, fix this!"
        )
        text = text[:400]
    ssml = f"""
    <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{call.lang.short_code}">
        <voice name="{call.lang.voice}" effect="eq_telecomhp8k">
            <lexicon uri="{CONFIG.resources.public_url}/lexicon.xml" />
            <mstts:express-as style="{style.value}" styledegree="0.5">
                <prosody rate="0.95">{text}</prosody>
            </mstts:express-as>
        </voice>
    </speak>
    """
    return SsmlSource(ssml_text=ssml.strip())


async def handle_recognize_ivr(
    client: CallConnectionClient,
    call: CallModel,
    text: str,
    choices: list[RecognitionChoice],
) -> None:
    _logger.info(f"Playing text before IVR: {text}")
    _logger.debug(f"Recognizing IVR")
    try:
        client.start_recognizing_media(
            choices=choices,
            end_silence_timeout=20,
            input_type=RecognizeInputType.CHOICES,
            interrupt_prompt=True,
            play_prompt=_audio_from_text(text, MessageStyleEnum.NONE, call),
            speech_language=call.lang.short_code,
            target_participant=PhoneNumberIdentifier(call.phone_number),  # type: ignore
        )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before recognizing")