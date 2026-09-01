import asyncio
import base64
import json
import os
from dataclasses import replace

import aiohttp
from livekit.agents import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DEFAULT_API_CONNECT_OPTIONS,
)
from livekit.agents import tts as agents_tts
from livekit.plugins.sarvam import tts as sarvam_tts


ALLOWED_OUTPUT_AUDIO_CODECS = {
    "mp3",
    "wav",
    "aac",
    "opus",
    "flac",
    "pcm",
    "mulaw",
    "alaw",
}

OUTPUT_AUDIO_CODEC_TO_MIME_TYPE = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "pcm": "audio/pcm",
    "mulaw": "audio/basic",
    "alaw": "audio/basic",
}


def _normalize_codec(codec: str | None) -> str:
    normalized = (codec or os.getenv("SARVAM_OUTPUT_AUDIO_CODEC", "wav")).strip().lower()
    return normalized if normalized in ALLOWED_OUTPUT_AUDIO_CODECS else "wav"


def _codec_mime_type(codec: str | None) -> str:
    return OUTPUT_AUDIO_CODEC_TO_MIME_TYPE[_normalize_codec(codec)]


def apply_sarvam_wav_patch() -> None:
    if getattr(sarvam_tts, "_vantarax_wav_patch_applied", False):
        return

    original_init = sarvam_tts.TTS.__init__
    original_update_options = sarvam_tts.TTS.update_options

    def patched_init(self, *args, output_audio_codec: str | None = None, **kwargs):
        codec = _normalize_codec(kwargs.pop("output_audio_codec", None) or output_audio_codec)
        original_init(self, *args, **kwargs)
        setattr(self._opts, "output_audio_codec", codec)

    def patched_update_options(self, *args, output_audio_codec: str | None = None, **kwargs):
        original_update_options(self, *args, **kwargs)
        if output_audio_codec is not None:
            setattr(self._opts, "output_audio_codec", _normalize_codec(output_audio_codec))

    async def patched_chunked_run(self, output_emitter: agents_tts.AudioEmitter) -> None:
        payload = {
            "target_language_code": self._opts.target_language_code,
            "text": self._input_text,
            "speaker": self._opts.speaker,
            "pace": self._opts.pace,
            "speech_sample_rate": self._opts.speech_sample_rate,
            "model": self._opts.model,
            "output_audio_codec": _normalize_codec(getattr(self._opts, "output_audio_codec", None)),
            "output_audio_bitrate": self._opts.output_audio_bitrate,
            "min_buffer_size": self._opts.min_buffer_size,
            "max_chunk_length": self._opts.max_chunk_length,
        }

        if self._opts.model == "bulbul:v2":
            payload["pitch"] = self._opts.pitch
            payload["loudness"] = self._opts.loudness
            payload["enable_preprocessing"] = self._opts.enable_preprocessing
        if self._opts.model in ("bulbul:v3", "bulbul:v3-beta"):
            payload["temperature"] = self._opts.temperature

        headers = {
            "api-subscription-key": self._opts.api_key,
            "Content-Type": "application/json",
            "User-Agent": sarvam_tts.USER_AGENT,
        }

        try:
            async with self._tts._ensure_session().post(
                url=self._opts.base_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=self._conn_options.timeout,
                    sock_connect=self._conn_options.timeout,
                ),
            ) as res:
                if res.status != 200:
                    error_text = await res.text()
                    sarvam_tts.logger.error(f"Sarvam TTS API error: {res.status} - {error_text}")
                    raise APIStatusError(
                        message=f"Sarvam TTS API Error ({res.status}): {error_text}",
                        status_code=res.status,
                        body=error_text,
                    )

                response_json = await res.json()
                request_id = response_json.get("request_id", "")
                audios = response_json.get("audios", [])
                if not audios or not isinstance(audios, list):
                    raise APIConnectionError("Sarvam TTS API response invalid: no audio data")

                codec = _normalize_codec(getattr(self._opts, "output_audio_codec", None))
                output_emitter.initialize(
                    request_id=request_id or "unknown",
                    sample_rate=self._tts.sample_rate,
                    num_channels=self._tts.num_channels,
                    mime_type=_codec_mime_type(codec),
                )

                for b64 in audios:
                    output_emitter.push(base64.b64decode(b64))
        except asyncio.TimeoutError as e:
            raise APITimeoutError("Sarvam TTS API request timed out") from e
        except aiohttp.ClientError as e:
            raise APIConnectionError(f"Sarvam TTS API connection error: {e}") from e

    async def patched_stream_run(self, output_emitter: agents_tts.AudioEmitter) -> None:
        request_id = sarvam_tts.utils.shortuuid()
        self._client_request_id = request_id
        self._server_request_id = None
        codec = _normalize_codec(getattr(self._opts, "output_audio_codec", None))
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._opts.speech_sample_rate,
            num_channels=1,
            mime_type=_codec_mime_type(codec),
            stream=True,
            frame_size_ms=50,
        )

        async def _tokenize_input() -> None:
            word_stream = None
            async for input in self._input_ch:
                if isinstance(input, str):
                    if word_stream is None:
                        tokenizer_instance = (
                            self._opts.word_tokenizer
                            if self._opts.word_tokenizer is not None
                            else sarvam_tts.tokenize.basic.SentenceTokenizer()
                        )
                        word_stream = tokenizer_instance.stream()
                        self._segments_ch.send_nowait(word_stream)
                    word_stream.push_text(input)
                elif isinstance(input, self._FlushSentinel):
                    if word_stream:
                        word_stream.end_input()
                    word_stream = None

            if word_stream is not None:
                word_stream.end_input()

            self._segments_ch.close()

        async def _process_segments() -> None:
            async for word_stream in self._segments_ch:
                await self._run_ws(word_stream, output_emitter)

        tasks = [
            asyncio.create_task(_tokenize_input()),
            asyncio.create_task(_process_segments()),
        ]
        try:
            await asyncio.gather(*tasks)
        except (APIStatusError, APIConnectionError, APITimeoutError):
            raise
        except asyncio.TimeoutError:
            raise APITimeoutError() from None
        except aiohttp.ClientResponseError as e:
            raise APIStatusError(
                message=e.message,
                status_code=e.status,
                request_id=request_id,
                body=None,
            ) from None
        except Exception as e:
            raise APIConnectionError(f"TTS stream failed: {e}") from e
        finally:
            await sarvam_tts.utils.aio.gracefully_cancel(*tasks)
            output_emitter.end_input()

    async def patched_run_ws(self, word_stream, output_emitter: agents_tts.AudioEmitter) -> None:
        segment_id = sarvam_tts.utils.shortuuid()
        output_emitter.start_segment(segment_id=segment_id)

        sarvam_tts.logger.info(
            "Starting TTS WebSocket session",
            extra={**self._build_log_context(), "user-agent": sarvam_tts.USER_AGENT},
        )

        async def send_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            try:
                config_msg = {
                    "type": "config",
                    "data": {
                        "target_language_code": self._opts.target_language_code,
                        "speaker": self._opts.speaker,
                        "pace": self._opts.pace,
                        "model": self._opts.model,
                        "output_audio_codec": _normalize_codec(
                            getattr(self._opts, "output_audio_codec", None)
                        ),
                    },
                }
                if self._opts.model == "bulbul:v2":
                    config_msg["data"]["pitch"] = self._opts.pitch
                    config_msg["data"]["loudness"] = self._opts.loudness
                    config_msg["data"]["enable_preprocessing"] = self._opts.enable_preprocessing
                if self._opts.model in ("bulbul:v3", "bulbul:v3-beta"):
                    config_msg["data"]["temperature"] = self._opts.temperature
                    config_msg["data"]["output_audio_bitrate"] = self._opts.output_audio_bitrate
                    config_msg["data"]["min_buffer_size"] = self._opts.min_buffer_size
                    config_msg["data"]["max_chunk_length"] = self._opts.max_chunk_length

                sarvam_tts.logger.debug(
                    "Sending TTS config",
                    extra={**self._build_log_context(), "config": config_msg},
                )
                await ws.send_str(json.dumps(config_msg))

                started = False
                async for word in word_stream:
                    if not started:
                        self._mark_started()
                        started = True
                    await ws.send_str(json.dumps({"type": "text", "data": {"text": word.token}}))

                await ws.send_str(json.dumps({"type": "flush"}))
            except Exception as e:
                sarvam_tts.logger.error(
                    f"Error in send task: {e}",
                    extra=self._build_log_context(),
                    exc_info=True,
                )
                raise APIConnectionError(f"Send task failed: {e}") from e

        async def recv_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            try:
                while True:
                    msg = await ws.receive(timeout=self._conn_options.timeout)

                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        close_code = ws.close_code if ws.close_code is not None else msg.data
                        close_reason = msg.extra
                        is_expected_close = close_code in (1000, 1001, None)
                        if not is_expected_close:
                            sarvam_tts.logger.error(
                                "WebSocket connection closed by server",
                                extra={
                                    **self._build_log_context(),
                                    "close_code": close_code,
                                    "close_reason": close_reason,
                                },
                            )
                            raw_close = {
                                "msg_type": str(msg.type),
                                "close_code": close_code,
                                "close_reason": close_reason,
                            }
                            raise APIStatusError(
                                message=(
                                    "Sarvam TTS WebSocket closed with non-graceful status: "
                                    f"{json.dumps(raw_close, ensure_ascii=False)}"
                                ),
                                status_code=int(close_code) if isinstance(close_code, int) else -1,
                                body=raw_close,
                            )
                        sarvam_tts.logger.info(
                            "WebSocket connection closed by server",
                            extra={
                                **self._build_log_context(),
                                "close_code": close_code,
                                "close_reason": close_reason,
                            },
                        )
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        success = await self._handle_websocket_message(msg.data, output_emitter)
                        if not success:
                            break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise APIConnectionError(f"WebSocket error: {msg.data}")
            except asyncio.TimeoutError as e:
                sarvam_tts.logger.error(
                    "WebSocket received timeout",
                    extra=self._build_log_context(),
                )
                raise APITimeoutError("WebSocket receive timeout") from e
            except Exception as e:
                sarvam_tts.logger.error(
                    f"Error in receive task: {e}",
                    extra=self._build_log_context(),
                    exc_info=True,
                )
                raise

        try:
            async with self._tts._pool.connection(timeout=self._conn_options.timeout) as ws:
                self._ws_conn = ws
                self._connection_state = sarvam_tts.ConnectionState.CONNECTED
                sarvam_tts.logger.info(
                    "WebSocket connected successfully",
                    extra=self._build_log_context(),
                )

                self._send_task = asyncio.create_task(send_task(ws))
                self._recv_task = asyncio.create_task(recv_task(ws))
                tasks = [self._send_task, self._recv_task]

                try:
                    await asyncio.gather(*tasks)
                    sarvam_tts.logger.info(
                        "WebSocket session completed successfully",
                        extra=self._build_log_context(),
                    )
                finally:
                    await sarvam_tts.utils.aio.gracefully_cancel(*tasks)
                    self._send_task = None
                    self._recv_task = None
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
            self._connection_state = sarvam_tts.ConnectionState.FAILED
            sarvam_tts.logger.error(f"Connection failed: {e}", extra=self._build_log_context())
            raise APIConnectionError(f"Failed to connect to TTS WebSocket: {e}") from e
        except (APIStatusError, APIConnectionError, APITimeoutError):
            self._connection_state = sarvam_tts.ConnectionState.FAILED
            raise
        except Exception as e:
            self._connection_state = sarvam_tts.ConnectionState.FAILED
            sarvam_tts.logger.error(
                f"Unexpected error in WebSocket session: {e}",
                extra=self._build_log_context(),
                exc_info=True,
            )
            raise APIStatusError(f"TTS WebSocket session failed: {e}") from e
        finally:
            self._connection_state = sarvam_tts.ConnectionState.DISCONNECTED
            self._ws_conn = None

    sarvam_tts.TTS.__init__ = patched_init
    sarvam_tts.TTS.update_options = patched_update_options
    sarvam_tts.ChunkedStream._run = patched_chunked_run
    sarvam_tts.SynthesizeStream._run = patched_stream_run
    sarvam_tts.SynthesizeStream._run_ws = patched_run_ws
    sarvam_tts._vantarax_wav_patch_applied = True

