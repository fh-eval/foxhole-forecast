Hello again! We're running a game predicting events in the video game Foxhole. Basically we're trying to see if you can predict which bases change and how. We're checking the game's official API roughly every ~15 minutes to see if the predictions are accurate. The window between checks isn't precise, so always make an exact UTC prediction for when the state-change happens (trying to game it will compound the noise).

Please return exactly eight ranked bets on eight different strategic bases from the supplied regions. Rank 1-4 are your immediate priorities and rank 5-8 are your extended priorities. Aim for ranks 1-4 to land within 6 hours and ranks 5-8 to land more than 6 hours out, but give the exact minute you actually believe: the evaluator will keep a valid bet when it is near that boundary.

For each base, predict what happens next by copying `outcome` from that base's `valid_outcomes`. `CAPTURED_BY_WARDENS` means the base will be captured by Wardens; `CAPTURED_BY_COLONIALS` means it will be captured by Colonials. Neutral bases (`current_owner` is `NONE`) must use one of those two faction-specific capture outcomes—do not use a generic capture call. `DESTROYED` means a faction-owned base is demolished and remains unbuilt until the next API check window. Give an exact-minute UTC ETA. Destruction followed by capture inside the same window counts as the named faction capture; choose `DESTROYED` if you expect the base to remain unbuilt into the next ~15 minute window. A destruction and a faction capture are distinct outcomes, but confusing them receives partial credit because a base change did happen.

For every bet, copy one value exactly from that base's own `valid_outcomes`. Here's how that works:

[Warden Controlled Base] -> `CAPTURED_BY_COLONIALS` or `DESTROYED` (Both options are predicting a Colonial push succeeding, right?).
[Colonial Controlled Base] -> `CAPTURED_BY_WARDENS` or `DESTROYED` (Again, same rationale. This means the Wardens successfully attacked the base).
[Neutral/Unowned Base] -> `CAPTURED_BY_COLONIALS` or `CAPTURED_BY_WARDENS` (If a base is up for grabs, one of the two teams will take it. Your bet should say who).

`confidence` is the probability from 0.00 to 1.00 that the exact named outcome happens by three hours after your ETA. `sigma_minutes` is your timing uncertainty conditional on that event happening: give the standard deviation in minutes of a Normal event-time distribution centered on your ETA. It must be an integer from 15 to 180. Use a smaller sigma only when you can locate the event time tightly; confidence and sigma measure different uncertainties.

Use only supplied metric IDs as evidence and rate each metric's relevance from 1 to 10. Please return only the requested JSON.

After making the eight bets, give four short, symmetric strategic recommendations using bases from the supplied packet:

- `colonial_reinforce`: one Colonial-owned base the Colonials should reinforce, and why.
- `colonial_attack`: one Warden-owned base the Colonials should attack, and why.
- `warden_reinforce`: one Warden-owned base the Wardens should reinforce, and why.
- `warden_attack`: one Colonial-owned base the Wardens should attack, and why.

For each recommendation, copy the exact `base_id`, write a concise reason, and cite 1-3 supplied metric IDs with relevance ratings in the same format as prediction evidence. You may choose a base from your eight bets, but you do not have to. Use only the packet. Do not invent stockpiles, troop movements, tactics, intentions, or intelligence the public data does not contain. These recommendations are collected as qualitative strategic-adviser evidence; they do not replace or alter the eight scoreable predictions, and we do not treat an unacted-upon recommendation as objectively proven right or wrong.
