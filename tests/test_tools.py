"""Calendar + agent tools against a real (SQLite) database."""
from __future__ import annotations

import pytest

from app.tools.calendar import SlotUnavailable
from app.tools.registry import ToolContext


async def test_available_slots_exclude_bookings(calendar, monday, at):
    before = await calendar.available_slots(monday, "dr-rao")
    assert at(monday, "09:00") in before
    await calendar.book(patient_name="Ravi", start=at(monday, "09:00"), provider_id="dr-rao")
    after = await calendar.available_slots(monday, "dr-rao")
    assert at(monday, "09:00") not in after
    assert len(after) == len(before) - 1
    # other doctor is unaffected
    assert at(monday, "09:00") in await calendar.available_slots(monday, "dr-lakshmi")


async def test_double_booking_rejected(calendar, monday, at):
    await calendar.book(patient_name="A", start=at(monday, "10:00"), provider_id="dr-rao")
    with pytest.raises(SlotUnavailable):
        await calendar.book(patient_name="B", start=at(monday, "10:00"), provider_id="dr-rao")
    with pytest.raises(SlotUnavailable):  # outside opening hours
        await calendar.book(patient_name="C", start=at(monday, "14:00"), provider_id="dr-rao")


async def test_check_availability_tool_filters_part_of_day(executor, ctx, monday):
    res = await executor.run(ctx, "check_availability", {"date": monday.isoformat(), "part_of_day": "evening",
                                                         "provider_id": "dr-rao"})
    assert res["available"] and all(t >= "17:00" for t in res["available"])
    assert res["weekday"] == "Monday"


async def test_book_then_find_then_cancel(executor, ctx, monday):
    res = await executor.run(ctx, "book_appointment", {"patient_name": "Sai Charan", "date": monday.isoformat(),
                                                        "time": "17:20", "provider_id": "Dr. Rao", "reason": "fever"})
    assert res["booked"] is True and res["provider"] == "Dr. Srinivasa Rao"
    assert ctx.outcome == "booked"

    found = await executor.run(ctx, "find_appointments", {})
    assert [a["appointment_id"] for a in found["appointments"]] == [res["appointment_id"]]

    other_caller = ToolContext(call_id="other", channel="phone", caller_phone="+919876543210")
    again = await executor.run(other_caller, "book_appointment", {"patient_name": "Xavier", "date": monday.isoformat(),
                                                                   "time": "17:20", "provider_id": "dr-rao"})
    assert again["booked"] is False and "17:40" in again["alternatives"]

    cancelled = await executor.run(ctx, "cancel_appointment", {"appointment_id": res["appointment_id"]})
    assert cancelled["cancelled"] is True
    assert (await executor.run(ctx, "find_appointments", {}))["appointments"] == []


async def test_closed_day_and_bad_input(executor, ctx, monday):
    sunday = monday.fromordinal(monday.toordinal() + 6)
    res = await executor.run(ctx, "check_availability", {"date": sunday.isoformat()})
    assert all(d["available"] == [] for d in res["doctors"]) and "closed" in res["note"]
    bad = await executor.run(ctx, "check_availability", {"date": "tomorrow"})
    assert "error" in bad
    assert "error" in await executor.run(ctx, "nope", {})


async def test_transfer_and_end_set_pending_action(executor, ctx):
    await executor.run(ctx, "transfer_to_human", {"reason": "wants a person"})
    assert ctx.pending_action == "transfer" and ctx.outcome == "transferred"
    await executor.run(ctx, "end_call", {})
    assert ctx.pending_action == "hangup"


async def test_no_doctor_chosen_lists_every_doctor(executor, ctx, monday, at, calendar):
    await calendar.book(patient_name="A", start=at(monday, "09:00"), provider_id="dr-rao")
    res = await executor.run(ctx, "check_availability", {"date": monday.isoformat(), "part_of_day": "morning"})
    names = {d["provider"]: d for d in res["doctors"]}
    assert set(names) == {"Dr. Srinivasa Rao", "Dr. Lakshmi Devi"}
    assert "09:00" not in names["Dr. Srinivasa Rao"]["available"]
    assert "09:00" in names["Dr. Lakshmi Devi"]["available"]
    assert names["Dr. Lakshmi Devi"]["specialty"] == "Paediatrician"
    assert "has not chosen a doctor" in res["instruction"]
    # booking without a doctor is refused so the assistant must ask
    r = await executor.run(ctx, "book_appointment", {"patient_name": "X", "date": monday.isoformat(), "time": "10:00"})
    assert r["booked"] is False and len(r["doctors"]) == 2


def test_provider_lookup_by_spoken_name(business):
    for spoken in ("dr-rao", "Dr. Rao", "Doctor Rao", "rao garu", "Srinivasa Rao"):
        assert business.provider(spoken).id == "dr-rao", spoken
    assert business.provider("Lakshmi Devi").id == "dr-lakshmi"
    assert business.provider("Dr. Sharma") is None


async def test_booking_requires_real_name_and_valid_phone(executor, monday):
    ctx = ToolContext(call_id="c1", channel="browser", caller_phone=None)  # web demo, number unknown
    base = {"date": monday.isoformat(), "time": "12:00", "provider_id": "dr-lakshmi"}

    r = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Unknown"})
    assert r["booked"] is False and r["reason"] == "patient name missing"

    r = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Sri Lakshmi"})
    assert r["booked"] is False and r["reason"] == "phone number missing"

    for bad in ("12345678910", "1234567890", "9999999999", "93469"):
        r = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Sri Lakshmi", "phone": bad})
        assert r["booked"] is False and r["reason"].startswith("invalid phone number"), bad

    r = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Sri Lakshmi", "phone": "93469 68532"})
    assert r["booked"] is True
    appt = await executor.cal.db.get_appointment(r["appointment_id"])
    assert appt.phone == "+919346968532" and appt.patient_name == "Sri Lakshmi"


async def test_same_call_rebooking_updates_instead_of_blocking(executor, ctx, monday):
    base = {"date": monday.isoformat(), "time": "12:00", "provider_id": "dr-lakshmi"}
    first = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Sri"})
    second = await executor.run(ctx, "book_appointment", {**base, "patient_name": "Sri Lakshmi"})
    assert second["booked"] is True and second["updated"] is True
    assert second["appointment_id"] == first["appointment_id"]
    appt = await executor.cal.db.get_appointment(first["appointment_id"])
    assert appt.patient_name == "Sri Lakshmi"


def test_validate_phone():
    from app.phone import validate_phone

    assert validate_phone("9346968532") == ("+919346968532", None)
    assert validate_phone("+91 93469 68532")[0] == "+919346968532"
    assert validate_phone("12345678910")[0] is None
    assert validate_phone("5346968532")[1] == "Indian mobile numbers start with 6, 7, 8 or 9"
    assert validate_phone("+14155550123")[0] == "+14155550123"


async def test_emergency_transfer_tells_caller_to_call_ambulance(executor, ctx):
    result = await executor.run(ctx, "transfer_to_human", {"reason": "chest pain", "emergency": True})
    assert ctx.pending_action == "transfer" and result["emergency"] is True
    assert "108" in result["instruction"]


async def test_find_appointments_by_spoken_number_or_name(executor, ctx, monday):
    from datetime import datetime
    biz = executor.biz
    await executor.cal.book(patient_name="Meena", phone="+919000011111", provider_id="dr-rao",
                            start=datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=biz.tz))
    ctx.caller_phone = "+919999999999"  # calling from a different phone
    assert (await executor.run(ctx, "find_appointments", {}))["appointments"] == []
    by_spoken = await executor.run(ctx, "find_appointments", {"phone": "90000 11111"})
    by_name = await executor.run(ctx, "find_appointments", {"name": "meena", "phone": "unknown"})
    assert len(by_spoken["appointments"]) == 1 and len(by_name["appointments"]) == 1


async def test_find_appointments_uses_caller_number_even_with_a_name(executor, ctx, monday):
    from datetime import datetime
    await executor.cal.book(patient_name="Meena", phone=ctx.caller_phone, provider_id="dr-rao",
                            start=datetime(monday.year, monday.month, monday.day, 9, 0, tzinfo=executor.biz.tz))
    result = await executor.run(ctx, "find_appointments", {"name": "Caller"})  # placeholder name from the model
    assert len(result["appointments"]) == 1


# ---------------------------------------------------------------- reschedule / ownership
async def _book(executor, ctx, monday, t="10:00", name="Ravi", **extra):
    return await executor.run(ctx, "book_appointment", {"patient_name": name, "date": monday.isoformat(),
                                                        "time": t, "provider_id": "dr-rao", **extra})


async def test_reschedule_moves_the_booking_and_frees_the_old_slot(executor, ctx, monday, db):
    first = await _book(executor, ctx, monday, "10:00")
    moved = await executor.run(ctx, "reschedule_appointment", {"appointment_id": first["appointment_id"],
                                                               "date": monday.isoformat(), "time": "11:00"})
    assert moved["rescheduled"] and moved["previous_appointment_cancelled"] == first["appointment_id"]
    active = await db.find_appointments()
    assert [a.id for a in active] == [moved["appointment_id"]]
    assert "10:00" in (await executor.run(ctx, "check_availability",
                                          {"date": monday.isoformat(), "provider_id": "dr-rao"}))["available"]


async def test_reschedule_to_a_taken_slot_keeps_the_original(executor, ctx, monday, db):
    first = await _book(executor, ctx, monday, "10:00")
    await _book(executor, ctx, monday, "11:00", name="Someone Else")
    r = await executor.run(ctx, "reschedule_appointment", {"appointment_id": first["appointment_id"],
                                                           "date": monday.isoformat(), "time": "11:00"})
    assert not r["rescheduled"] and r["alternatives"]
    assert (await db.get_appointment(first["appointment_id"])).status == "booked"


async def test_same_patient_second_booking_is_redirected_to_reschedule(executor, ctx, monday, db):
    first = await _book(executor, ctx, monday, "10:00")
    second = await _book(executor, ctx, monday, "11:00")
    assert not second["booked"] and second["existing"]["appointment_id"] == first["appointment_id"]
    assert "reschedule_appointment" in second["instruction"]
    extra = await _book(executor, ctx, monday, "11:00", second_appointment=True)
    assert extra["booked"] and len(await db.find_appointments()) == 2


async def test_cannot_see_or_change_another_patients_booking(executor, ctx, monday, db):
    from datetime import datetime
    other = await executor.cal.book(patient_name="Meena Iyer", phone="+919811122233", provider_id="dr-rao",
                                    start=datetime(monday.year, monday.month, monday.day, 9, 0,
                                                   tzinfo=executor.biz.tz))
    found = await executor.run(ctx, "find_appointments", {"name": "Meena"})
    assert found["appointments"] == [] and "different mobile number" in found["instruction"]
    for tool_name, args in [("cancel_appointment", {"appointment_id": other.id}),
                            ("reschedule_appointment", {"appointment_id": other.id,
                                                        "date": monday.isoformat(), "time": "11:00"})]:
        r = await executor.run(ctx, tool_name, args)
        assert r["reason"] == "not the caller's appointment"
    assert (await db.get_appointment(other.id)).status == "booked"
    # ...but saying the number used when booking proves ownership
    mine = await executor.run(ctx, "find_appointments", {"name": "Meena", "phone": "98111 22233"})
    assert mine["appointments"][0]["appointment_id"] == other.id
    assert (await executor.run(ctx, "cancel_appointment", {"appointment_id": other.id}))["cancelled"]
