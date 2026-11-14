# JARVIS — Voice

Phase 7. Capture and playback stay in the browser the user already has open.
Nothing is installed, and there is no wake word.

## What is implemented

The web client has a hold-to-talk control and an English/Hindi switch. Holding
the control starts the browser's speech recognition in the locale returned by
`GET /api/v1/voice/session`. Releasing it stops listening. A finished transcript
is submitted as a voice command to `POST /api/v1/assistant/commands` with
`source: web` and the open conversation id. That is the same planner and the
same queue as Siri and Google Assistant. Typed messages stay on the conversation
stream and are not spoken back. If the voice submit fails, the transcript is
put back in the message field so it can be sent as text.

`POST /api/v1/voice/speak` shortens a finished reply to the 240-character voice
limit and returns the text plus a BCP-47 language tag. The browser speaks that
text with its own speech synthesis. The response `audio` field is `null`: this
API does not generate audio.

When the browser has no speech recognition, the client tells the user to type.
That is the text fallback, and `text_fallback` is always true: typing remains
valid even if a server speech credential is added later. A missing credential is
reported separately. `stt_configured` and `tts_configured` are false on
`/api/v1/health`, and no transcript is invented. A voice command whose locale is
not English or Hindi is rejected with 422 before planning.

## What is not implemented

`STT_PROVIDER` and `TTS_PROVIDER` are accepted as configuration and reported in
health. No vendor implementation is wired, because no credential is configured
and none was invented. A future provider implements `transcribe` and
`synthesize` behind `app/ai/speech.py` and does not change the command gateway.

Wake-word activation is not possible under the zero-install constraint and is
not built. No microphone permission is requested until the user holds the
control.
