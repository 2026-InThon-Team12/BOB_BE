# Expo integration (2026-11)

`SimulationSummary.observation_dates` exposes the exact observation calendar in item registration, lookup and decision responses. The mobile client must use these dates rather than assuming the original start plus duration is contiguous: late HOLD creates a gap and resumes tomorrow. A same-day sale deadline returns an empty calendar. No database migration is needed for this response-only addition.

The Expo client stores server item/simulation/purchase IDs. LOCAL questions and raw answers stay on-device; only the result-summary request uploads the completed transcript, and the server stores aggregates only. BUY/HOLD/GIVE_UP and the 14-day usage review use the owned server IDs. Existing records without server IDs remain local and must not be matched to remote records by name alone.

Android Expo Go supports the app's local reminders, not remote FCM. Installed Android builds register a native FCM token and handle client-managed data notifications by checking local unanswered work. Device delivery still requires a configured native build and a separate device test.

Contract regressions: `tests/test_expo_contract.py` covers exact observation dates, late HOLD, idempotent replay and a same-day deadline. Existing suites cover ownership, local questions, aggregate summaries, purchase reviews and notifications.
