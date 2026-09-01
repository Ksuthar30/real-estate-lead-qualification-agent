import json

import agent


def run_controller_turn(
    state: agent.CallState,
    text: str,
    analysis: agent.SalesTurnAnalysis | None = None,
):
    normalized = agent.update_state_from_user_input(text, state)
    if analysis:
        agent.apply_sales_turn_analysis(state, analysis)
    action = agent.decide_next_action(state, normalized)
    semantic_non_off_topic = state.last_turn_type in {
        "answer",
        "permission",
        "repair",
        "identity_question",
        "call_purpose_question",
        "business_question",
        "objection",
        "rejection",
        "callback_request",
        "wrong_lead",
        "confusion",
    }
    off_topic = state.last_turn_type == "off_topic" or (
        agent.is_off_topic_user_input(text, normalized) and not semantic_non_off_topic
    )
    reply = agent.validate_llm_response_for_voice(
        agent.render_controller_fallback_response(state, action, normalized, off_topic)
    )
    forced = agent.should_force_deterministic_response(state, action, normalized)
    assert reply, f"empty reply for {text!r}"
    assert reply.count("?") <= 1, f"multiple questions for {text!r}: {reply}"
    assert len(reply.split()) <= 20, f"too long for {text!r}: {reply}"
    return normalized, action, reply, forced, off_topic


def test_first_not_interested_is_handled_as_objection_not_blind_close():
    state = agent.CallState()
    analysis = agent.SalesTurnAnalysis(
        turn_type="rejection",
        disposition="not_interested",
        objection_type="",
        reason="customer says not interested",
        semantic_summary="generic first not interested without hard stop",
        confidence=0.92,
    )

    normalized, action, reply, forced, _ = run_controller_turn(
        state,
        "not interested",
        analysis,
    )

    assert normalized.soft_rejection is True
    assert state.lead_disposition is None
    assert state.not_interested is False
    assert state.objection_type == "not_interested_reflex"
    assert state.last_turn_type == "objection"
    assert action["current_step"] == "interest"
    assert action["should_ask"] is True
    assert forced is False

    normalized, action, reply, forced, _ = run_controller_turn(state, "not interested")
    assert state.lead_disposition == "not_interested"
    assert state.not_interested is True
    assert action["current_step"] == "done"
    assert action["should_ask"] is False


def test_flirting_is_semantic_redirection_without_deterministic_brain_bypass():
    state = agent.CallState()
    analysis = agent.SalesTurnAnalysis(
        turn_type="off_topic",
        question_type="personal",
        direct_answer="Main property enquiry mein help kar rahi hoon",
        semantic_summary="customer is flirting and asking personal question",
        buyer_signal="personal",
        sales_move="redirect",
        response_strategy="boundary then property question",
        confidence=0.95,
    )

    normalized, action, reply, forced, off_topic = run_controller_turn(
        state,
        "I love you, are you single?",
        analysis,
    )

    assert off_topic is True
    assert state.lead_disposition is None
    assert action["current_step"] == "interest"
    assert action["should_ask"] is True
    assert forced is False
    assert "property" in reply.lower() or "dekh" in reply.lower()


def test_price_pressure_is_reasoned_sales_turn_not_fixed_keyword_only():
    state = agent.CallState()
    analysis = agent.SalesTurnAnalysis(
        turn_type="business_question",
        question_type="price",
        disposition="interested",
        interest_level="warm",
        objection_type="price",
        next_action="capture_requirement",
        direct_answer="Budget clear karna zaroori hai",
        semantic_summary="customer asks price before giving requirement",
        buyer_signal="objection",
        sales_move="clarify",
        response_strategy="acknowledge budget then qualify BHK",
        confidence=0.9,
    )

    normalized, action, reply, forced, _ = run_controller_turn(
        state,
        "pehle price batao warna time waste mat karo",
        analysis,
    )

    assert state.lead_disposition == "interested"
    assert state.objection_type == "price"
    assert action["current_step"] == "bhk"
    assert action["should_ask"] is True
    assert forced is False
    assert "bhk" in reply.lower()


def test_mixed_hinglish_extracts_multiple_real_estate_signals():
    state = agent.CallState()
    analysis = agent.SalesTurnAnalysis(
        turn_type="answer",
        disposition="interested",
        interest_level="warm",
        objection_type="budget",
        next_action="send_details_or_follow_up",
        bhk="2BHK",
        timeline="3 months",
        semantic_summary="needs 2BHK in Panvel, budget cautious, three-month plan",
        buyer_signal="warm",
        sales_move="qualify",
        response_strategy="acknowledge and close update",
        confidence=0.93,
    )

    _, action, reply, forced, _ = run_controller_turn(
        state,
        "mujhe 2 bhk Panvel me chahiye, budget tight hai, 3 mahine mein plan kar raha hoon",
        analysis,
    )

    assert state.lead_disposition == "interested"
    assert state.bhk == "2 BHK"
    assert state.timeline in {"3 months", "3 mahine"}
    assert action["current_step"] == "done"
    assert action["should_ask"] is False
    assert forced is False


def test_safety_turns_still_bypass_llm_for_live_call_recovery():
    for text in ("Muy bien.", "haan boliye", "kis regarding aapne phone kiya hai"):
        state = agent.CallState()
        normalized, action, reply, forced, _ = run_controller_turn(state, text)
        assert action["should_ask"] is True
        assert forced is True
        assert "property" in reply.lower() or "regarding" in reply.lower()


def main() -> None:
    tests = [
        test_first_not_interested_is_handled_as_objection_not_blind_close,
        test_flirting_is_semantic_redirection_without_deterministic_brain_bypass,
        test_price_pressure_is_reasoned_sales_turn_not_fixed_keyword_only,
        test_mixed_hinglish_extracts_multiple_real_estate_signals,
        test_safety_turns_still_bypass_llm_for_live_call_recovery,
    ]
    for test in tests:
        test()
    print(json.dumps({"status": "VOICE_AGENT_COMPLEX_CONVERSATION_TESTS_OK", "tests": len(tests)}))


if __name__ == "__main__":
    main()
