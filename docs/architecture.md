# Architecture and design notes

## Goals

1. **Feel like a human on the phone:** short replies, fast responses, interruptible.
2. **Indian languages first:** English, Hindi and Telugu, with code-mixing.
3. **Safe to run for a real business:** no medical advice, human handoff, auth, limits.
4. **Swappable vendors:** every AI service sits behind a ~20-line interface.

## Components

| Module | Responsibility |
|---|---|
| `transports/twilio.py` | Answers the Twilio webhook with `<Connect><Stream>`, decodes μ-law 8 kHz audio, sends agent audio back as `media` events, sends `clear` on barge-in, transfers or hangs up calls through the REST API |
| `transports/browser.py` | The same contract for browsers: PCM16 16 kHz binary frames plus JSON UI events |
| `pipeline/session.py` | `CallSession`: owns one call, runs the STT event loop, end-of-turn timer, response task, tool loop, playback clock and metrics |
| `providers/*` | `STTProvider` / `LLMProvider` / `TTSProvider` implementations (Sarvam, Deepgram, OpenAI) |
| `tools/registry.py` | OpenAI function schemas and executors; tools never raise into the call |
| `tools/calendar.py` | Slot engine (opening hours minus bookings minus Google busy time) and double-booking protection |
| `knowledge/*` | RAG: chunking (.md/.txt/.pdf with heading-path citations), embeddings, hybrid BM25 + vector retrieval fused with RRF (pgvector on Postgres, NumPy elsewhere), folder sync and uploads |
| `tools/registry.py` ownership rules | Callers can only see/change bookings made in the call or under a phone number they control |

## Turn-taking

```
STT events            speech_started ──► (agent busy?) ──► BARGE-IN: cancel response, clear audio
                      final(text)    ──► append to user buffer, update language
                      utterance_end  ──► start end-of-turn timer (350 ms)
                      speech_started during timer ──► cancel timer (caller is still talking)
timer fires           ──► user turn complete ──► start response task
```

The end-of-turn grace period stops the agent from replying when the caller is only pausing mid-sentence. You can tune it with `END_OF_TURN_DELAY_MS`.

## Response streaming and latency budget

```
caller stops ──► STT final (~200-500 ms) ──► grace (350 ms) ──► LLM first sentence (~300-600 ms)
             ──► TTS first audio (~150-300 ms) ──► caller hears reply
```

These are typical published ranges, not VaaniDesk measurements. Measure your own: the dashboard reports per-turn **end-of-turn → first audio byte** latency.

Techniques used to keep latency low:

- **Sentence-level streaming:** `SentenceChunker` releases each sentence (or a long clause at a comma) to TTS right away. It recognises `. ! ? । ॥` and avoids splitting "Dr. Rao" or "5.30".
- **One TTS WebSocket per reply:** sentences are pushed as they arrive and audio streams back in chunks.
- **Fire-and-forget side effects:** SMS sending never blocks the conversation.
- **No audio transcoding services:** the μ-law codec and resampling are done in numpy.

## Barge-in

When the caller interrupts:

1. The response task is cancelled, which closes the TTS stream and stops the LLM stream.
2. `clear_audio()` tells Twilio (or the browser) to drop buffered audio.
3. The part of the reply already spoken is saved to history with `[interrupted by caller]`, so the LLM knows what the caller heard.
4. `_repair_history()` makes sure every tool call has a result, even if a tool round was cut short (the OpenAI API requires this).

## Multilingual strategy

- **STT:** Sarvam `language-code=unknown` auto-detects the language of each utterance. The detected language is stored on the call.
- **LLM:** the prompt tells it to answer in the caller's language and script (Devanagari or Telugu).
- **TTS:** the voice language is chosen from the **script of the reply text**, not from the STT guess. That makes it robust when STT mislabels code-mixed speech.

## Safety

- Policies in `business.yaml`: no medical advice, emergencies go to 108 and a human.
- `transfer_to_human` and `end_call` run only **after** the current sentence finishes playing.
- Twilio webhook signatures are validated (HMAC-SHA1).
- The dashboard uses HTTP Basic auth. The public demo can require an access code, and `MAX_CONCURRENT_CALLS` caps concurrent calls.
- A watchdog ends calls after `MAX_CALL_SECONDS`.

## Data model

- `calls`: channel, caller, language, status, outcome, AI summary, average latency, interruptions
- `turns`: user, assistant and tool turns with per-turn latency and tool arguments and results
- `appointments`: patient, phone, provider, start and end (stored in UTC), status
