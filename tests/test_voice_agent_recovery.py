import json
import asyncio
import re

import agent


def run_turn(state: agent.CallState, text: str):
    normalized = agent.update_state_from_user_input(text, state)
    action = agent.decide_next_action(state, normalized)
    reply = agent.validate_llm_response_for_voice(
        agent.render_controller_fallback_response(
            state,
            action,
            normalized,
            agent.is_off_topic_user_input(text, normalized),
        )
    )
    assert reply, f"empty reply for {text!r}"
    assert reply.count("?") <= 1, f"multiple questions for {text!r}: {reply}"
    assert len(reply.split()) <= 20, f"too long for {text!r}: {reply}"
    return normalized, action, reply


def test_permission_repairs_do_not_close_or_increment_attempts():
    for text in ("Muy bien.", "┬┐Hello?", "Hello hello", "haan boliye"):
        state = agent.CallState()
        normalized, action, reply = run_turn(state, text)
        assert action["current_step"] == "interest"
        assert action["should_ask"] is True
        assert state.done_reason is None
        assert state.lead_disposition is None
        assert state.step_attempts.get("interest", 0) == 0
        assert normalized.short_ack or normalized.hello_repair
        assert "property" in reply.lower() or "dekh" in reply.lower()


def test_low_confidence_semantic_does_not_poison_state():
    state = agent.CallState()
    analysis = agent.SalesTurnAnalysis(
        turn_type="answer",
        question_type="none",
        disposition="unclear",
        next_action="send_details_or_follow_up",
        direct_answer="Aapko property ke bare mein pata chalna hai?",
        semantic_summary="Customer responded with a non-English phrase, unclear intention",
        confidence=0.2,
    )
    agent.apply_sales_turn_analysis(state, analysis)
    assert state.last_turn_type == "repair"
    assert state.lead_disposition is None
    assert state.next_action is None
    assert state.done_reason is None

    normalized, action, reply = run_turn(state, "Hello?")
    assert action["current_step"] == "interest"
    assert action["should_ask"] is True
    assert state.done_reason is None


def test_real_terminal_intents_still_close():
    terminal_cases = {
        "maine koi enquiry nahi ki": "wrong_lead",
        "call me later": "callback_later",
    }
    for text, expected in terminal_cases.items():
        state = agent.CallState()
        normalized, action, reply = run_turn(state, text)
        assert state.lead_disposition == expected
        assert action["current_step"] == "done"
        assert action["should_ask"] is False
        assert action["done_reason"] == expected
        assert "thank" in reply.lower() or "sorry" in reply.lower()


def test_opening_is_clear_ascii_permission_first():
    greeting = agent._build_outbound_greeting()
    assert greeting == (
        "Hi, this is Riya from Proviso Group. "
        "You had a property enquiry. Is this a good time?"
    )
    assert "?" in greeting
    assert len(greeting.split()) <= 18
    assert not re.search(r"[^\x00-\x7F]", greeting)
    assert agent._sanitize_spoken_text(greeting, agent.CallState()).endswith("good time?")


def test_runtime_prompt_does_not_request_hinglish_speech():
    instructions = agent._build_agent_instructions({})
    assert "Hinglish" not in instructions
    assert "Hindi goodbye" not in instructions
    assert "kar dungi" not in instructions
    assert "simple Indian English" in instructions


def test_english_confusion_switches_to_simple_english():
    state = agent.CallState()
    normalized, action, reply = run_turn(state, "English")
    assert normalized.language_request is True
    assert state.language_mode == "english"
    assert "English" in reply
    assert "property" in reply.lower()

    normalized, action, reply = run_turn(state, "I don't understand what you're saying")
    assert normalized.audio_confusion is True
    assert state.confusion_count >= 1
    assert reply.startswith("Sorry")
    assert "property" in reply.lower() or "thank" in reply.lower()


def test_repeated_confusion_closes_without_repeating_sales_question():
    state = agent.CallState()
    run_turn(state, "I don't understand what you're saying")
    normalized, action, reply = run_turn(state, "voice not clear")
    assert action["current_step"] == "done"
    assert action["done_reason"] == "unclear_interest"
    assert "thank" in reply.lower()
    assert "property" not in reply.lower()


def test_hard_stop_closes_immediately():
    state = agent.CallState()
    normalized, action, reply = run_turn(state, "I don't want to talk to you")
    assert normalized.hard_stop is True
    assert state.lead_disposition == "not_interested"
    assert action["current_step"] == "done"
    assert action["should_ask"] is False
    assert "thank" in reply.lower()


def test_recording_noise_turns_do_not_call_semantic_llm():
    async def run_noise_case(text: str):
        state = agent.CallState()
        normalized = agent.update_state_from_user_input(text, state)
        assert normalized.stt_noise is True
        analysis = await agent._analyze_sales_turn(text, state, "groq")
        assert analysis is None
        action = agent.decide_next_action(state, normalized)
        reply = agent.render_controller_fallback_response(state, action, normalized)
        assert reply
        assert "property" in reply.lower() or "thank" in reply.lower()

    for text in ("you", "one", "angolia", "bodith", "you borro", "This.", "Theek Ek this."):
        asyncio.run(run_noise_case(text))


def test_real_call_short_yes_advances_interest_question():
    state = agent.CallState()
    normalized, action, reply = run_turn(state, "this")
    assert normalized.stt_noise is True
    assert action["current_step"] == "interest"
    assert state.lead_disposition is None

    normalized, action, reply = run_turn(state, "Yes is.")
    assert state.lead_disposition == "interested"
    assert action["current_step"] == "bhk"
    assert "BHK" in reply


def test_real_call_noisy_bhk_is_extracted():
    state = agent.CallState(lead_disposition="interested", current_step="bhk", last_question="bhk")
    normalized, action, reply = run_turn(state, "PDHK, Ji guess.")
    assert normalized.bhk == "2 BHK"
    assert state.bhk == "2 BHK"
    assert action["current_step"] == "timeline"
    assert "planning" in reply.lower()

    state = agent.CallState(lead_disposition="interested", current_step="bhk", last_question="bhk")
    normalized, action, reply = run_turn(state, "I want who PHP.")
    assert normalized.bhk == "2 BHK"
    assert action["current_step"] == "timeline"


def test_repeated_bhk_repairs_close_as_interested_missing_detail():
    state = agent.CallState()
    run_turn(state, "This, I'm looking for the property.")
    assert state.lead_disposition == "interested"
    assert state.step_attempts.get("bhk") == 1

    normalized, action, reply = run_turn(state, "You triste")
    assert action["current_step"] == "bhk"
    assert "BHK" in reply

    normalized, action, reply = run_turn(state, "dejan")
    assert action["current_step"] == "done"
    assert action["done_reason"] == "interested_missing_detail"
    assert "WhatsApp" in reply


def main() -> None:
    tests = [
        test_permission_repairs_do_not_close_or_increment_attempts,
        test_low_confidence_semantic_does_not_poison_state,
        test_real_terminal_intents_still_close,
        test_opening_is_clear_ascii_permission_first,
        test_runtime_prompt_does_not_request_hinglish_speech,
        test_english_confusion_switches_to_simple_english,
        test_repeated_confusion_closes_without_repeating_sales_question,
        test_hard_stop_closes_immediately,
        test_recording_noise_turns_do_not_call_semantic_llm,
        test_real_call_short_yes_advances_interest_question,
        test_real_call_noisy_bhk_is_extracted,
        test_repeated_bhk_repairs_close_as_interested_missing_detail,
    ]
    for test in tests:
        test()
    print(json.dumps({"status": "VOICE_AGENT_RECOVERY_TESTS_OK", "tests": len(tests)}))


if __name__ == "__main__":
    main()
