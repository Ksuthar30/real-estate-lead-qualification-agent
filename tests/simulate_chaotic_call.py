import asyncio
import copy
import json
import time

import agent


CHAOTIC_CALL = [
    "Hello? Kaun?",
    "Haan dekha tha ad somewhere... but honestly abhi sirf explore kar raha hu.",
    "Waise location kaha hai exactly?",
    "Budget depends yaar... pehle property toh acchi honi chahiye.",
    "Wait wait... tum AI ho kya?",
    "😂 sounds human actually. Tum single ho kya?",
    "Achha leave that... 3bhk maybe. Family ke liye dekh raha hu.",
    "But possession kab hai?",
    "Actually Gurgaon bhi dekh raha hu and Noida bhi.",
    "Aap log broker ho ya builder side se?",
    "Site visit nahi kiya but friend ne bola tha project acha hai.",
    "EMI around 70-80 manageable hai maybe.",
    "Not immediately buying though. Maybe 6 months. Maybe earlier if good deal.",
    "Hello? Hello? Your voice broke.",
    "Haan haan samajh gaya, but price batao na roughly.",
    "Agar negotiation hua toh best price kya ho sakta hai?",
    "Actually ek baat batao honestly — market abhi down hai na?",
    "Parents convincing issue bhi hai thoda.",
    "Send brochure on WhatsApp maybe.",
    "Okay gotta go now.",
]


SEMANTIC_NON_OFF_TOPIC = {
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


async def phrase_response(
    state: agent.CallState,
    action: dict,
    normalized: agent.NormalizedUserInput,
    analysis: agent.SalesTurnAnalysis | None,
    user_text: str,
    off_topic: bool,
    fallback: str,
    force_deterministic: bool,
) -> tuple[str, str, float]:
    start = time.perf_counter()
    source = "fallback"
    spoken = fallback
    if (
        not force_deterministic
        and fallback
        and agent.should_use_fast_semantic_response(state, action, normalized)
    ):
        source = "fast_semantic_response"
    elif not force_deterministic and action.get("should_ask"):
        chat_ctx = agent.llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content=agent._build_agent_instructions({}))
        chat_ctx.add_message(
            role="system",
            content=agent.build_controller_context(
                state,
                action,
                normalized,
                latest_user_text=user_text,
                sales_analysis=analysis,
                off_topic=off_topic,
            ),
        )
        response_llm = agent._build_llm(max_completion_tokens=64)
        try:
            response = await response_llm.chat(chat_ctx=chat_ctx).collect()
            spoken = agent.validate_llm_response_for_voice(response.text)
            source = "llm"
        except Exception as exc:
            spoken = fallback
            source = f"llm_exception_fallback:{type(exc).__name__}"
        if action.get("should_ask") and "?" not in spoken:
            spoken = fallback
            source = "fallback_missing_question"

    sanitized = agent._sanitize_spoken_text(spoken, state)
    final_spoken = agent.validate_llm_response_for_voice(sanitized)
    if action.get("should_ask") and "?" not in final_spoken and fallback:
        final_spoken = fallback
        source = "fallback_after_validation"
    return final_spoken, source, round((time.perf_counter() - start) * 1000, 1)


async def main() -> None:
    state = agent.CallState()
    turns = []
    greeting = agent._build_outbound_greeting()
    for index, user_text in enumerate(CHAOTIC_CALL, start=1):
        if index > 1:
            await asyncio.sleep(1.0)
        turn_start = time.perf_counter()

        normalized = agent.update_state_from_user_input(user_text, state)
        semantic_start = time.perf_counter()
        analysis = await agent._analyze_sales_turn(user_text, state, None)
        semantic_ms = round((time.perf_counter() - semantic_start) * 1000, 1)
        agent.apply_sales_turn_analysis(state, analysis)
        action = agent.decide_next_action(state, normalized)
        off_topic = state.last_turn_type == "off_topic" or (
            agent.is_off_topic_user_input(user_text, normalized)
            and state.last_turn_type not in SEMANTIC_NON_OFF_TOPIC
        )
        fallback = agent.validate_llm_response_for_voice(
            agent.render_controller_fallback_response(state, action, normalized, off_topic)
        )
        force_deterministic = agent.should_force_deterministic_response(
            state,
            action,
            normalized,
        )
        spoken, response_source, response_ms = await phrase_response(
            state,
            action,
            normalized,
            analysis,
            user_text,
            off_topic,
            fallback,
            force_deterministic,
        )

        snapshot = copy.deepcopy(state.public_state())
        turns.append(
            {
                "turn": index,
                "user": user_text,
                "analysis": analysis.model_dump() if analysis else None,
                "normalized": {
                    key: value
                    for key, value in normalized.__dict__.items()
                    if value not in (None, False, "", {}, [])
                },
                "action": action,
                "off_topic": off_topic,
                "force_deterministic": force_deterministic,
                "response_source": response_source,
                "agent": spoken,
                "state": snapshot,
                "timing_ms": {
                    "semantic": semantic_ms,
                    "response": response_ms,
                    "total": round((time.perf_counter() - turn_start) * 1000, 1),
                },
                "qa_flags": {
                    "empty_agent": not bool(spoken),
                    "multiple_questions": spoken.count("?") > 1,
                    "too_long": len(spoken.split()) > 20,
                    "missing_required_question": bool(action.get("should_ask")) and "?" not in spoken,
                    "deterministic_non_repair": force_deterministic
                    and state.last_turn_type not in {
                        "permission",
                        "repair",
                        "identity_question",
                        "call_purpose_question",
                        "confusion",
                    }
                    and not (normalized.hello_repair or normalized.short_ack or normalized.confusion),
                    "repeated_step": len(state.step_attempts) > 0
                    and max(state.step_attempts.values()) > 1,
                    "done_before_end": action.get("current_step") == "done" and index < len(CHAOTIC_CALL),
                    "latency_risk": (semantic_ms + response_ms) > 3500,
                },
            }
        )

    output = {
        "greeting": greeting,
        "turns": turns,
        "final_state": state.public_state(),
    }
    with open("chaotic_simulation_latest.json", "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "output_file": "chaotic_simulation_latest.json",
                "turns": len(turns),
                "qa_flag_counts": {
                    flag: sum(1 for turn in turns if turn["qa_flags"].get(flag))
                    for flag in turns[0]["qa_flags"]
                }
                if turns
                else {},
                "final_state": state.public_state(),
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
