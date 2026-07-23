# Speech and Transcription

Agency exposes speech input in two layers:

- `agency.speech.listen` is a built-in agent tool for recorded audio files, voice-note chunks, or base64 audio payloads. It calls the configured speech-to-text provider and returns normalized text plus optional verbose metadata.
- `POST /speech/realtime/transcription-session` creates an OpenAI Realtime transcription client secret for a transcription session. Browser clients should use this for live microphone streaming, then send completed transcript turns into the normal conversation API.

Speech input and transcription are separate from generated voice output. Use `agency.voice.generate` when a workflow
needs to synthesize spoken audio from text, and use `agency.media.send` only when that generated artifact should be
delivered to a tied application. See [Voice And Media Tools](tools.md#voice-and-media-tools).

## Configuration

Set these environment variables on the backend:

```env
OPENAI_API_KEY=sk-...
OPENAI_API_BASE_URL=https://api.openai.com/v1
OPENAI_AUDIO_TRANSCRIPTION_MODEL=whisper-1
OPENAI_REALTIME_TRANSCRIPTION_MODEL=whisper-1
AGENCY_VISION_PROVIDER=local
AGENCY_VISION_MODEL=
```

`whisper-1` is the default for the requested Whisper path. OpenAI also supports newer transcription models such as `gpt-4o-mini-transcribe` and `gpt-4o-transcribe`; those can be selected through the model inputs or environment defaults.

`AGENCY_VISION_PROVIDER` and `AGENCY_VISION_MODEL` configure the generic image-analysis helper used by vision-capable routes and tools. The default `local` provider returns deterministic local analysis when no external vision model is configured.

## Recorded Audio Tool

Example tool input:

```json
{
  "audio_base64": "BASE64_AUDIO_BYTES",
  "filename": "speech.webm",
  "language": "en",
  "response_format": "json"
}
```

Exactly one of `file_path` or `audio_base64` is required.

## Realtime Flow

1. The authenticated frontend calls `POST /speech/realtime/transcription-session`.
2. The backend returns OpenAI's realtime client-secret payload, including an ephemeral `value` and the configured transcription session.
3. The frontend streams microphone audio to OpenAI Realtime using that ephemeral secret.
4. Completed transcript turns are submitted to Agency conversations as user messages.

Keep raw microphone streaming in the browser-to-OpenAI Realtime channel. The backend should mint short-lived sessions and keep the long-lived `OPENAI_API_KEY` server-side only.
