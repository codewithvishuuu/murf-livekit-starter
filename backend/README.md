# Backend — Voice Agent with Murf Falcon TTS

The Python backend for the Voice Agent Starter. It runs a real-time voice AI pipeline using [LiveKit Agents](https://docs.livekit.io/agents), connecting Murf Falcon TTS, Deepgram STT, and Google Gemini into a single conversational agent.

## How It Works

```
User speaks → [Deepgram STT] → text → [Gemini LLM] → response → [Murf Falcon TTS] → audio → User hears
```

LiveKit handles the real-time audio transport. The agent connects to LiveKit as a participant, listens for user speech, and responds with synthesized audio.

## Setup

### 1. Install dependencies

```bash
cd backend
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env.local
```

Fill in your keys in `.env.local`:

| Variable             | Where to get it                                           |
| -------------------- | --------------------------------------------------------- |
| `LIVEKIT_URL`        | [LiveKit Cloud](https://cloud.livekit.io/) → Settings     |
| `LIVEKIT_API_KEY`    | [LiveKit Cloud](https://cloud.livekit.io/) → Settings     |
| `LIVEKIT_API_SECRET` | [LiveKit Cloud](https://cloud.livekit.io/) → Settings     |
| `MURF_API_KEY`       | [murf.ai/api/dashboard](https://murf.ai/api/dashboard)    |
| `DEEPGRAM_API_KEY`   | [deepgram.com](https://console.deepgram.com/)             |
| `GOOGLE_API_KEY`     | [aistudio.google.com](https://aistudio.google.com/apikey) |

For **outbound calling (Day 6)** you also need a LiveKit Cloud outbound SIP trunk:

| Variable                       | Where to get it                                              |
| ------------------------------ | ------------------------------------------------------------ |
| `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` | LiveKit Cloud → your project → **SIP → Trunks** (outbound trunk ID) |

Optional outbound tuning (defaults shown) — see `.env.example`:

| Variable                          | Default           | Purpose                                             |
| --------------------------------- | ----------------- | --------------------------------------------------- |
| `OUTBOUND_DIAL_NUMBER`            | —                 | Number dialed when the CLI is run without an argument |
| `OUTBOUND_AGENT_NAME`             | `my-agent`        | Agent dispatched into the outbound room             |
| `OUTBOUND_RINGING_TIMEOUT_S`      | `30`              | Seconds to ring before giving up                    |
| `OUTBOUND_MAX_CALL_DURATION_S`    | `300`             | Safety cap for an unsupervised test call            |
| `OUTBOUND_CALLER_NAME`            | `Aarogya Sahayak` | Name spoken in the outbound opening                 |

> **Security:** never commit `LIVEKIT_API_SECRET`, `MURF_API_KEY`, or the real
> outbound trunk ID. The dialing utility only ever reads these from the
> environment and prints `set`/`MISSING` status — never the values.

For LiveKit Cloud users, you can auto-populate LiveKit credentials:

```bash
lk cloud auth
lk app env -w -d .env.local
```

### 3. Download models

```bash
uv run python src/agent.py download-files
```

This downloads Silero VAD and the LiveKit turn detector models.

### 4. Run the agent

```bash
# Development mode (auto-reload)
uv run python src/agent.py dev

# Or test directly in your terminal (no frontend needed)
uv run python src/agent.py console

# Production
uv run python src/agent.py start
```

## Configuration

All configuration lives in [`src/agent.py`](src/agent.py).

### System prompt

The `SYSTEM_PROMPT` constant at the top of `agent.py` controls what your agent does. Change it to build any voice-powered use case.

#### Example prompts

**Customer Support (default):**

```
You are a friendly and efficient customer support agent for a tech company. Help users with account issues, billing questions, and product troubleshooting. Be concise, empathetic, and solution-oriented. If you don't know something, say so honestly and offer to escalate.
```

**Language Tutor:**

```
You are a patient and encouraging language tutor helping the user practice conversational Spanish. Speak primarily in Spanish but switch to English to explain grammar or vocabulary when needed. Correct mistakes gently and suggest better phrasing. Keep conversations natural and fun.
```

**AI Receptionist:**

```
You are a professional receptionist for a medical clinic. Help callers schedule appointments, answer questions about office hours and services, and take messages for doctors. Be warm but efficient. Ask for the caller's name and reason for calling upfront.
```

**Interview Coach:**

```
You are an experienced interview coach. Conduct mock interviews with the user for software engineering roles. Ask one behavioral or technical question at a time, let the user answer fully, then give specific feedback on their response — what was strong, what could improve, and a suggested reframe. Keep the tone encouraging but honest.
```

**Sales Assistant:**

```
You are a knowledgeable sales assistant for an electronics store. Help customers find the right product by asking about their needs, budget, and preferences. Compare options clearly, highlight trade-offs, and make a recommendation. Never be pushy — focus on helping the customer make the best decision for them.
```

**Fitness Coach:**

```
You are an upbeat personal fitness coach. Help users plan workouts, suggest exercises for specific muscle groups, and answer questions about form and technique. Ask about their fitness level and any injuries before recommending exercises. Keep instructions clear and motivating.
```

**Storyteller / Bedtime Narrator:**

```
You are a creative storyteller who tells original bedtime stories for children aged 4–8. Ask the child (or parent) for a character name, a favorite animal, and a setting, then weave a short, calming story. Use vivid but simple language. End each story on a peaceful, sleepy note.
```

**Meeting Summarizer:**

```
You are a meeting assistant. The user will describe what happened in a meeting or read you their notes. Summarize the key decisions, action items (with owners if mentioned), and any open questions. Be concise and structured. Ask clarifying questions if something is ambiguous.
```

**Trivia Game Host:**

```
You are an enthusiastic trivia game host. Ask the user one trivia question at a time from a mix of categories — science, history, pop culture, geography, and sports. Wait for their answer, tell them if they're right or wrong, give a brief fun fact, then move to the next question. Keep score and announce it every 5 questions.
```

**Mental Health Check-in Companion:**

```
You are a gentle, non-clinical wellness companion. Help users talk through their day, reflect on how they're feeling, and practice simple grounding exercises like deep breathing or gratitude lists. You are not a therapist — if the user expresses serious distress or mentions self-harm, gently encourage them to reach out to a professional or crisis helpline.
```

### Voice

Set the `voice` argument in the `murf.TTS(...)` call:

```python
tts=murf.TTS(
    voice="en-US-matthew",    # Change this
    style="Conversation",
    tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=2),
    text_pacing=True
)
```

Some voice options:

| Voice ID | Description                      |
| -------- | -------------------------------- |
| `Anisha` | Indian English, female (default) |
| `Pooja`  | Indian English, female           |
| `Samar`  | Indian English, male             |
| `Amara`  | US English, female               |
| `Hazel`  | UK English, female               |
| `Bertie` | UK English, male                 |
| `Gordon` | US English, male                 |

Browse all 150+ voices: [Murf Voice Library](https://murf.ai/api/docs/voices-styles/voice-library).

### STT (Speech-to-Text)

Default is Deepgram Nova-3. Change in the `AgentSession(stt=...)` call:

```python
stt=deepgram.STT(model="nova-3")
```

### LLM

Default is Google Gemini. To switch:

- **Gemini (default):** Set `GOOGLE_API_KEY` in `.env.local`
- **OpenAI:** Set `OPENAI_API_KEY`, install `livekit-agents[openai]`, and change the `llm=` argument

### Healthcare Facility Lookup (Day 5)

The agent can look up healthcare facilities (government health centres, PHCs, CHCs, hospitals, clinics, dispensaries, sub-centres) through the `find_health_facilities` tool.

- **Data source:** the public [OpenStreetMap Overpass API](https://overpass-api.de/) (`https://overpass-api.de/api/interpreter`, with a backup mirror at `overpass.kumi.systems`) — **live at query time**.
- **Data origin:** community-maintained OpenStreetMap data, including government health facilities imported from data.gov.in by the OSM India Health Facilities Import. It is **not government-verified** and may be incomplete or outdated.
- **No API key required.** Optional tuning: `HEALTH_FACILITIES_TIMEOUT_S` (per-request timeout in seconds, default 10).
- **Failure behavior:** timeouts, network errors, rate limits, and invalid responses return a natural spoken fallback ("the facility data service is temporarily unavailable"); empty results tell the caller no matching facilities were found and to confirm with their District Health Office. The agent never invents facilities, names, phones, or addresses.
- **Freshness:** the tool reports the Overpass `timestamp_osm_base` ("the facility data was last refreshed on …") when available; otherwise it honestly says the community-maintained data may not be fully up to date.
- **Attribution:** OpenStreetMap contributors, © OpenStreetMap (ODbL). System prompt and tool description instruct the agent to keep results spoken and natural.

### Outbound Calling (Day 6)

The same Aarogya Sahayak agent can **place an outbound call** for a healthcare
follow-up (appointment or medication reminder). No second agent is involved —
the dialer just launches the existing `my-agent` into a dedicated room:

```
python -m telephony.outbound <phone>  →  create room
                                      →  dial via outbound SIP trunk (wait for answer)
                                      →  dispatch my-agent into the room
                                      →  agent speaks its outbound opening and the call runs
                                      →  room cleaned up when the call ends
```

The outbound opening always states **who** is calling (Aarogya Sahayak), **why**
(a scheduled appointment/medication reminder), and **how to end the call**
(say "end the call" / "stop", or just hang up). The agent is instructed to
never be pushy and to accept opt-out immediately.

**Start the agent** (must be running so the dialer can dispatch it):

```bash
cd backend
uv run python src/agent.py dev
```

**Place ONE test call** (from another terminal):

```bash
cd backend
uv run python -m telephony.outbound '+91XXXXXXXXXX'
```

**Real PSTN calls.** The dialer is provider-agnostic: any LiveKit outbound
trunk works. For a real phone call, create an outbound trunk in LiveKit Cloud
(SIP → Trunks) connected to a PSTN SIP provider (Twilio, Telnyx, SignalWire,
...) that supplies an authorised caller ID number, put its trunk ID in
`LIVEKIT_SIP_OUTBOUND_TRUNK_ID` (or `LIVEKIT_SIP_TRUNK_ID`), and dial an
E.164 destination, e.g.:

```bash
cd backend
uv run python -m telephony.outbound '+919876543210'
```

A working PSTN call always requires such a provider trunk — code alone cannot
place a call on the public phone network.

**Direct SIP-user calls (no PSTN gateway).** For a SIP registrar trunk (for
example a Linphone Free SIP account at `sip.linphone.org`), pass the SIP user
— LiveKit dials `user@<trunk address>`, so this reaches
`sip:vishal_demo123@sip.linphone.org`:

```bash
cd backend
uv run python -m telephony.outbound 'vishal_demo123'
```

The full SIP URI also works as a convenience — its user part is what gets
dialed, provided the host matches your trunk (`SIP_OUTBOUND_HOST`, if set):

```bash
cd backend
uv run python -m telephony.outbound 'sip:vishal_demo123@sip.linphone.org'
```

> Note: free SIP services such as Linphone Free SIP are a demonstration path.
> The account must be registered and online, and the service may still reject
> automated calls (reported as `unavailable`). For a dependable, submission
> demo use a real PSTN outbound trunk as described above.

- Add `--dry-run` to verify configuration and number validity **without** dialing
  (prints only `set`/`MISSING`, never secret values). A placeholder number such
  as `+91XXXXXXXXXX` is accepted by the dry-run so the whole configuration can
  be checked before a real number is configured.
- Add `--no-wait` to return as soon as the call is answered instead of monitoring
  until hang-up. The phone number can also come from `OUTBOUND_DIAL_NUMBER`.
- No automatic redialing is implemented: one explicit call per run only.

**Outcomes and graceful handling:**

| Situation                 | Behaviour                                                                  |
| ------------------------- | -------------------------------------------------------------------------- |
| Missing/invalid number    | Refused with `invalid_phone_number` before anything is dialed              |
| Missing trunk/config      | Refused with `missing_config` before anything is dialed                    |
| Busy / unavailable / rejected | `CALL FAILED reason=busy|unavailable|rejected` and the room is deleted  |
| No answer (ring timeout)  | `CALL FAILED reason=no_answer` and the room is deleted                     |
| Immediate hang-up         | Reported as `immediate hang-up`                                            |
| Agent fails to join       | Reported; call continues until the far end disconnects                     |
| Duration cap              | Room (call) is torn down at `OUTBOUND_MAX_CALL_DURATION_S` (default 300 s) |

**Day 6 use case (Health Access):** healthcare follow-up / appointment and
medication reminders, with full consent handling — the agent states who is
calling at pick-up, explains why, and immediately honours "stop" / "do not
call me again".

**Safety / consent notes:**
- Only dial numbers you are authorised to call. Outbound calling can ring a
  stranger's phone and incur carrier charges — validate the contact premise
  and get consent before automating.
- The opening must disclose the caller identity, purpose and opt-out; see
  `OUTBOUND_OPENING` in `src/prompt.py`.
- Credentials and the trunk ID are read from the environment only and never
  logged or printed; the dialer reports `set`/`MISSING`.
- The duration cap and per-call room teardown bound the blast radius of a
  misconfigured test call; redial loops are intentionally not implemented.

### Testing

The project includes an eval suite based on the LiveKit Agents [testing framework](https://docs.livekit.io/agents/build/testing/):

```bash
uv run pytest
```

Tests are in [`tests/test_agent.py`](tests/test_agent.py) and use LLM-as-judge evaluations to verify the agent behaves correctly (friendly greetings, grounding, refusing harmful requests). Day 5 facility tests live in [`tests/test_health_facilities.py`](tests/test_health_facilities.py), Day 4 memory tests in [`tests/test_memory.py`](tests/test_memory.py), and Day 6 outbound-dialing tests in [`tests/test_outbound.py`](tests/test_outbound.py) — the outbound tests inject a fake LiveKit client and never place a real call.

To run tests in CI, you'll need to add `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` as repository secrets.

## Deployment

### Railway

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/tIVCF1?referralCode=cNjn2P&utm_medium=integration&utm_source=template&utm_campaign=generic)

Set these environment variables in Railway:

- `MURF_API_KEY`
- `DEEPGRAM_API_KEY`
- `GOOGLE_API_KEY`
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`

### Docker

A production-ready [Dockerfile](Dockerfile) is included:

```bash
docker build -t murf-voice-agent .
docker run --env-file .env.local murf-voice-agent
```

## Project Structure

```
backend/
├── src/
│   ├── agent.py          # Agent entrypoint — pipeline, prompt, config
│   ├── prompt.py         # Aarogya Sahayak system prompt + outbound opening
│   ├── memory.py         # Day 4: caller memory store
│   ├── health_facilities.py  # Day 5: Overpass-based facility lookup
│   └── telephony/
│       └── outbound.py   # Day 6: outbound dialing utility + CLI
├── tests/
│   ├── test_agent.py     # LLM-judged eval suite
│   ├── test_outbound.py  # Day 6 dialing tests (fake client, no real calls)
│   ├── test_health_facilities.py  # Day 5 tests
│   └── test_memory.py    # Day 4 tests
├── .env.example           # Environment variable template
├── pyproject.toml         # Python dependencies (uv)
├── Dockerfile             # Production container
└── railway.toml           # Railway deploy config
```

## Links

- [Murf Falcon TTS Docs](https://murf.ai/api/docs/text-to-speech/streaming)
- [Murf Voice Library](https://murf.ai/api/docs/voices-styles/voice-library)
- [LiveKit Agents Docs](https://docs.livekit.io/agents)
- [Deepgram Nova-3 Docs](https://developers.deepgram.com)

## License

MIT — see [LICENSE](LICENSE).
