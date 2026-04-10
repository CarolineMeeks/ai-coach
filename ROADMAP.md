# Roadmap

This file is the parking lot for work we are not doing right now, but do not want to lose.

## Near Term

- Durable hosted storage for `coach.db`
- Generic goals and check-ins instead of hard-coded Fitbit-only goals
- Goal recognition in the UI and chat so the coach notices wins automatically
- Manual tracking for water intake
- Better handling of partial or stale Fitbit data in chat
- Hosted environment cleanup so local and hosted config stay separate

## Coaching Features

- More specific strength training guidance
- Daily "wins so far" summary
- Better detection of completed exercise, walks, and goal completion
- Recovery-aware next-day planning
- Protein target guidance that shows up in the daily summary
- Better celebration language when goals are met
- Proactive outreach for high-value moments like morning plans, goal wins, and evening reminders
- Workout history tracking so the coach can learn exercise patterns and compare soreness over time
- Symptom-aware overrides when soreness, fatigue, or pain should veto the cheerful Fitbit-only answer

## Manual Tracking

- Water intake
- Food macros
- Protein intake
- Supplements
- Symptom tracking around Zepbound dose days
- Sleep quality notes
- Manual goal completion by replying to coach prompts

## Product Foundation

- User profile and preferences that shape coaching tone and recommendations
- More flexible goal definitions with units, cadence, and data source
- Check-in model that supports both automatic and manual progress updates
- Better audit trail of what advice was given and why

## Messaging

- Two-way SMS via Twilio for reminders and check-ins
- Start with structured replies for important actions like shot logging, water, protein, and recovery walks
- Add confirmation logic before logging ambiguous free-text replies
- Later expand to full conversational SMS once state handling is trustworthy
- Support event-driven nudges instead of noisy generic reminders

## Multi-User Direction

- Multiple users with separate goals and integrations
- Support for friends running their own instances
- Clean migration path from SQLite to a managed database
- Login/auth strategy for an internet-facing version

## Gamification / Adventure Layer

- Points for completed goals once goal detection is trustworthy
- Streaks and weekly consistency rewards
- Shared adventures influenced by each person's real-world progress
- Team or party mechanics for friends
- Integration with an external gamification app

## Open Questions

- Which goals should be automatic versus manual first?
- What should count as a "win" on a recovery day?
- How should we handle goals that are weekly instead of daily?
- When should the coach celebrate versus push for more?
