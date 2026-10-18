"""End-to-end tests of the real-time conversation engine using fake providers."""
from __future__ import annotations

import asyncio

from app.pipeline.session import CallSession, SessionDeps
from app.providers.base import STTEvent
from app.tools.registry import ToolContext

from .fakes import FakeLLM, FakeSTT, FakeTransport, FakeTTS, text, tool


async def wait_for(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def make_session(db, business, executor, llm, tts=None, language="en-IN"):
    call = await db.create_call(channel="browser", caller="+919999999999")
    ctx = ToolContext(call_id=call.id, channel="browser", caller_phone="+919999999999", language=language)
    stt = FakeSTT()
    deps = SessionDeps(business=business, db=db, stt=stt, llm=llm, tts=tts or FakeTTS(), tools=executor,
                       end_of_turn_delay_ms=10)
    transport = FakeTransport()
    session = CallSession(deps, transport, ctx)
    await session.start()
    return session, transport, stt.streams[0]


async def say(stt, text_, language="en-IN"):
    await stt.push(STTEvent("speech_started"), STTEvent("final", text=text_, language=language),
                   STTEvent("utterance_end", language=language))


async def test_greeting_is_spoken(db, business, executor):
    session, transport, _ = await make_session(db, business, executor, FakeLLM())
    await wait_for(lambda: len(transport.audio) > 0)
    assert transport.of_type("assistant")[0]["text"].startswith("Hello!")
    await session.close()


async def test_booking_conversation_with_tools(db, business, executor, monday):
    d = monday.isoformat()
    llm = FakeLLM([
        tool("check_availability", "c1", date=d, part_of_day="evening", provider_id="dr-rao"),
        text("Dr. Rao is free at five o'clock", " or five twenty. Which one suits you?"),
        tool("book_appointment", "c2", patient_name="Sai", date=d, time="17:00", provider_id="dr-rao"),
        text("Done! Your appointment is booked for Monday at five. You'll get an SMS."),
    ])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())

    await say(stt, "I want an appointment on Monday evening")
    await wait_for(lambda: any(e.get("final") and "Which one" in e.get("text", "") for e in transport.of_type("assistant")))
    tool_events = transport.of_type("tool")
    assert tool_events[0]["name"] == "check_availability"
    assert "17:00" in tool_events[0]["result"]["available"]

    # The LLM saw the tool result in the right message order
    second_call = llm.calls[1]
    assert second_call[-2]["tool_calls"][0]["function"]["name"] == "check_availability"
    assert second_call[-1]["role"] == "tool"

    await say(stt, "Five o'clock please, my name is Sai")
    await wait_for(lambda: session.ctx.outcome == "booked")
    await wait_for(lambda: session._response.done())
    assert transport.of_type("metrics"), "latency should be reported for each turn"
    await session.close()

    appts = await db.find_appointments(phone="+919999999999")
    assert len(appts) == 1 and appts[0].patient_name == "Sai"


async def test_barge_in_cancels_speech(db, business, executor):
    # Slow TTS so the agent is still "speaking" when the caller interrupts.
    llm = FakeLLM([text("This is a very long answer. " * 5), text("Sure, go ahead.")], delay=0.02)
    session, transport, stt = await make_session(db, business, executor, llm, tts=FakeTTS(chunk_delay=0.05))
    await wait_for(lambda: session._response.done())

    await say(stt, "Tell me about the clinic")
    await wait_for(lambda: transport.of_type("assistant") and not transport.of_type("assistant")[-1].get("final"))
    await stt.push(STTEvent("speech_started"))  # caller interrupts
    await wait_for(lambda: transport.of_type("interrupted"))
    assert transport.cleared >= 1
    assert session.interruptions == 1
    await wait_for(lambda: session._response.done())

    await say(stt, "Actually, what are your timings?")
    await wait_for(lambda: any("go ahead" in e.get("text", "") for e in transport.of_type("assistant")))
    # interrupted reply is kept in history, marked as interrupted
    assert any("[interrupted by caller]" in (m.get("content") or "") for m in llm.calls[-1])
    await session.close()


async def test_language_follows_caller_and_reply_script(db, business, executor):
    llm = FakeLLM([text("తప్పకుండా, మీకు ఏ రోజు కావాలి?")])
    tts = FakeTTS()
    session, transport, stt = await make_session(db, business, executor, llm, tts=tts)
    await wait_for(lambda: session._response.done())
    await say(stt, "నాకు రేపు అపాయింట్‌మెంట్ కావాలి", language="te-IN")
    greeting_streams = len(tts.streams)  # the greeting uses one voice per language
    await wait_for(lambda: len(tts.streams) == greeting_streams + 1 and tts.streams[-1].texts)
    assert session.ctx.language == "te-IN"
    assert tts.streams[-1].language == "te-IN"
    await session.close()


async def test_end_call_hangs_up_after_goodbye(db, business, executor):
    llm = FakeLLM([text("Thank you for calling, goodbye!") + tool("end_call", "c9"), text("")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "That's all, thanks")
    await wait_for(lambda: transport.hung_up, timeout=5)
    await wait_for(lambda: session.closed.is_set())


async def test_typed_text_input(db, business, executor):
    llm = FakeLLM([text("We are open from nine in the morning.")])
    session, transport, _ = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await session.feed_text("What time do you open?")
    await wait_for(lambda: any("nine" in e.get("text", "") and e.get("final") for e in transport.of_type("assistant")))
    await session.close()


async def test_llm_failure_does_not_kill_call(db, business, executor):
    def boom(messages):
        raise RuntimeError("LLM down")

    llm = FakeLLM([boom, text("Back online, how can I help?")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "Hello?")
    await wait_for(lambda: transport.of_type("error"))
    assert "LLM down" in transport.of_type("error")[0]["message"]
    await say(stt, "Hello again")
    await wait_for(lambda: any("Back online" in e.get("text", "") for e in transport.of_type("assistant")))
    await session.close()


async def test_tool_call_extra_fields_are_echoed_back(db, business, executor, monday):
    from app.providers.base import LLMChunk, ToolCall

    sig = {"extra_content": {"google": {"thought_signature": "SIG=="}}}
    llm = FakeLLM([[LLMChunk(tool_calls=[ToolCall("c1", "check_availability", {"date": monday.isoformat()}, sig)])],
                   text("We have slots.")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "Any slots on Monday?")
    await wait_for(lambda: len(llm.calls) >= 2)
    sent = [m for m in llm.calls[1] if m.get("tool_calls")][0]["tool_calls"][0]
    assert sent["extra_content"] == sig["extra_content"] and sent["id"] == "c1"
    await session.close()


async def test_short_utterance_does_not_switch_language(db, business, executor):
    """A name or 'okay' mislabelled by STT must not flip the call language (seen in a live test)."""
    llm = FakeLLM([text("May I have your name?"), text("Thank you, Raju."), text("Goodbye!")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "I want an appointment", language="en-IN")
    await wait_for(lambda: len(llm.calls) >= 1 and session._response.done())
    await say(stt, "రాజు.", language="te-IN")          # name transcribed in Telugu script
    await wait_for(lambda: len(llm.calls) >= 2 and session._response.done())
    assert session.ctx.language == "en-IN"
    assert llm.calls[-1][-1]["content"] == "రాజు."           # caller text sent unchanged
    assert "Reply only in English" in llm.calls[-1][0]["content"]  # language rule lives in the system prompt
    await say(stt, "Okay.", language="hi-IN")           # one word mislabelled as Hindi
    await wait_for(lambda: len(llm.calls) >= 3)
    assert session.ctx.language == "en-IN"
    await session.close()


async def test_full_sentence_switches_language(db, business, executor):
    llm = FakeLLM([text("Sure."), text("సరే.")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "ఇంగ్లీష్ వద్దు తెలుగులో మాట్లాడండి", language="te-IN")
    await wait_for(lambda: len(llm.calls) >= 1)
    assert session.ctx.language == "te-IN"
    assert "Reply only in Telugu" in llm.calls[-1][0]["content"]
    await session.close()


async def test_echoed_instruction_is_never_spoken(db, business, executor):
    llm = FakeLLM([text("[Caller language: English. Reply only in English.] ", "Hello! Which doctor would you like?")])
    tts = FakeTTS()
    session, transport, stt = await make_session(db, business, executor, llm, tts=tts)
    await wait_for(lambda: session._response.done())
    await say(stt, "I want to book an appointment")
    await wait_for(lambda: any(e.get("final") and "Which doctor" in e.get("text", "") for e in transport.of_type("assistant")))
    spoken = " ".join(t for s in tts.streams for t in s.texts)
    assert "Caller language" not in spoken
    assert all("Caller language" not in e.get("text", "") for e in transport.of_type("assistant"))
    await session.close()


async def test_waits_longer_when_caller_stops_mid_sentence(db, business, executor):
    llm = FakeLLM([text("Sure, for which day?")])
    session, transport, stt = await make_session(db, business, executor, llm)
    session.d.incomplete_turn_delay_ms = 400
    await wait_for(lambda: session._response.done())
    await say(stt, "I want to book an appointment for")
    await asyncio.sleep(0.15)
    assert len(llm.calls) == 0  # still waiting for the rest of the sentence
    await say(stt, "tomorrow morning")  # caller continues -> one combined turn
    await wait_for(lambda: len(llm.calls) == 1)
    assert llm.calls[0][-1]["content"] == "I want to book an appointment for tomorrow morning"
    await session.close()


async def test_doctor_name_in_english_letters_keeps_hindi_call(db, business, executor):
    """Seen live: Hindi call, caller says 'Lakshmi Devi is.' (STT writes it in Latin) -> must stay Hindi."""
    llm = FakeLLM([text("ठीक है।"), text("ठीक है।"), text("ठीक है।"), text("Sure.")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "मेरे को कल अपॉइंटमेंट करना चाहिए।", language="hi-IN")
    await wait_for(lambda: len(llm.calls) >= 1 and session._response.done())
    assert session.ctx.language == "hi-IN"
    await say(stt, "Lakshmi Devi is.", language="en-IN")
    await wait_for(lambda: len(llm.calls) >= 2 and session._response.done())
    assert session.ctx.language == "hi-IN"
    await say(stt, "Doctor Lakshmi tomorrow morning okay please", language="en-IN")
    await wait_for(lambda: len(llm.calls) >= 3 and session._response.done())
    assert session.ctx.language == "hi-IN"
    assert "Reply only in Hindi" in llm.calls[-1][0]["content"]
    # a real English sentence still switches
    await say(stt, "Actually can we please continue this conversation in English", language="en-IN")
    await wait_for(lambda: len(llm.calls) >= 4)
    assert session.ctx.language == "en-IN"
    await session.close()


async def test_greeting_tells_callers_every_language_in_its_own_voice(db, business, executor):
    tts = FakeTTS()
    session, transport, _ = await make_session(db, business, executor, FakeLLM(), tts=tts)
    await wait_for(lambda: session._response.done())
    shown = transport.of_type("assistant")[0]["text"]
    assert shown.startswith("Hello! Thank you for calling Sunrise Family Clinic")
    assert "नमस्ते! Sunrise Family Clinic में कॉल करने के लिए धन्यवाद" in shown
    assert "నమస్కారం! Sunrise Family Clinic కి కాల్ చేసినందుకు ధన్యవాదాలు" in shown
    assert [s.language for s in tts.streams] == ["en-IN", "hi-IN", "te-IN"]
    await session.close()


async def test_filler_is_spoken_while_a_tool_runs(db, business, executor, monday):
    llm = FakeLLM([tool("check_availability", "c1", date=monday.isoformat(), provider_id="dr-rao"),
                   text("Dr. Rao is free at ten.")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "Is Dr. Rao free on Monday?")
    await wait_for(lambda: any(e.get("final") and "free at ten" in e["text"] for e in transport.of_type("assistant")))
    final = [e for e in transport.of_type("assistant") if e.get("final")][-1]["text"]
    assert final.startswith("One moment, please.")  # said before the tool ran
    # ...and the model sees it in its history, so it won't repeat it
    assert any(m["role"] == "assistant" and m.get("content") == "One moment, please." and m.get("tool_calls")
               for m in session.messages)
    await session.close()


async def test_filler_uses_the_callers_language(db, business, executor, monday):
    llm = FakeLLM([tool("check_availability", "c1", date=monday.isoformat(), provider_id="dr-rao"),
                   text("సోమవారం పది గంటలకు ఖాళీ ఉంది.")])
    session, transport, stt = await make_session(db, business, executor, llm, language="te-IN")
    await wait_for(lambda: session._response.done())
    await say(stt, "సోమవారం డాక్టర్ రావు గారు ఉన్నారా?", language="te-IN")
    await wait_for(lambda: any(e.get("final") and "ఖాళీ" in e["text"] for e in transport.of_type("assistant")))
    final = [e for e in transport.of_type("assistant") if e.get("final")][-1]["text"]
    assert final.startswith("ఒక్క క్షణం")
    await session.close()


async def test_no_filler_when_model_already_spoke_or_for_goodbye(db, business, executor):
    llm = FakeLLM([text("Thank you for calling, goodbye!") + tool("end_call", "c9"), text("")])
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "That's all, bye")
    await wait_for(lambda: transport.hung_up)
    assert not any("One moment" in e["text"] for e in transport.of_type("assistant"))
    await session.close()


async def test_tts_is_connected_while_llm_thinks_and_unused_stream_closed(db, business, executor):
    tts = FakeTTS()
    llm = FakeLLM([text("नमस्ते, बताइए।")], delay=0.05)  # caller is English, reply comes in Hindi
    session, transport, stt = await make_session(db, business, executor, llm, tts=tts)
    await wait_for(lambda: session._response.done())
    before = len(tts.streams)
    await say(stt, "Hi")
    await wait_for(lambda: len(tts.streams) > before)  # pre-opened before the LLM produced anything
    assert tts.streams[before].language == "en-IN" and not tts.streams[before].texts
    await wait_for(lambda: any(e.get("final") and "नमस्ते" in e["text"] for e in transport.of_type("assistant")))
    await wait_for(lambda: session._response.done())
    assert tts.streams[before].closed  # the unused English stream was cleaned up
    assert tts.streams[-1].language == "hi-IN"
    await session.close()


async def test_latency_is_time_to_first_audio_not_whole_turn(db, business, executor, monday):
    llm = FakeLLM([tool("check_availability", "c1", date=monday.isoformat(), provider_id="dr-rao"),
                   text("Dr. Rao is free at ten.")], delay=0.3)  # every LLM chunk is slow
    session, transport, stt = await make_session(db, business, executor, llm)
    await wait_for(lambda: session._response.done())
    await say(stt, "Is Dr. Rao free on Monday?")
    await wait_for(lambda: transport.of_type("metrics"), timeout=5)
    # The filler plays after one slow LLM round (~0.3 s), not after both rounds (~0.6 s).
    assert transport.of_type("metrics")[-1]["latency_ms"] < 550
    await session.close()
