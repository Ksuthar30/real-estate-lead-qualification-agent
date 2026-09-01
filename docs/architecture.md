# Architecture

VantaraX is an outbound voice-agent system with five primary layers:

1. **Telephony** — LiveKit and Vobiz provide rooms, SIP connectivity, outbound dialing, and transfers.
2. **Speech** — Deepgram handles speech recognition; Sarvam, Deepgram, or Cartesia can provide voice synthesis.
3. **Conversation control** — The agent combines an LLM with explicit lead-qualification and recovery logic.
4. **Business integration** — Lead outcomes can be written to Google Sheets.
5. **Operations and testing** — Docker configuration, trunk utilities, stress simulations, and voice-quality checks support development.

The repository is production-oriented but remains under active development. Credentials and customer call data must always stay outside source control.
