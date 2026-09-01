import asyncio
import json

import agent


CASES = [
    "not interested",
    "pehle price batao warna time waste mat karo",
    "I love you, are you single?",
    "aap single ho kya, voice bahut achi hai",
    "mujhe 2 bhk Panvel me chahiye budget tight hai 3 mahine me plan hai",
    "family se discuss karna padega, 2bhk hai budget 80 ke aas paas",
    "maine koi enquiry nahi ki",
    "call me later",
]


async def main() -> None:
    results = []
    for text in CASES:
        state = agent.CallState()
        normalized = agent.update_state_from_user_input(text, state)
        analysis = await agent._analyze_sales_turn(text, state, None)
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
        fallback = agent.validate_llm_response_for_voice(
            agent.render_controller_fallback_response(state, action, normalized, off_topic)
        )
        force_deterministic = agent.should_force_deterministic_response(
            state,
            action,
            normalized,
        )
        spoken = fallback
        if not force_deterministic and action.get("should_ask"):
            chat_ctx = agent.llm.ChatContext.empty()
            chat_ctx.add_message(role="system", content=agent._build_agent_instructions({}))
            chat_ctx.add_message(
                role="system",
                content=agent.build_controller_context(
                    state,
                    action,
                    normalized,
                    latest_user_text=text,
                    sales_analysis=analysis,
                    off_topic=off_topic,
                ),
            )
            response_llm = agent._build_llm(max_completion_tokens=64)
            response = await response_llm.chat(chat_ctx=chat_ctx).collect()
            spoken = agent.validate_llm_response_for_voice(response.text)
            if action.get("should_ask") and "?" not in spoken:
                spoken = fallback
        results.append(
            {
                "text": text,
                "analysis": analysis.model_dump() if analysis else None,
                "action": action,
                "state": state.public_state(),
                "force_deterministic": force_deterministic,
                "spoken": spoken,
            }
        )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
