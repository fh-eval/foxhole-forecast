Hello! We're running a prediction game for the video game Foxhole. This first stage is the broad war-desk overview.

Write `headline` as a concise, factual newspaper headline of roughly 4–12 words. Give it a little 1920s–1940s field-report character, but do not use a dateline, quotation marks, invented claims, or factional cheerleading.

Write `war_summary` as a compact 1920s–1940s newspaper dispatch: lively, economical, and readable, with the air of a correspondent filing the latest edition. This is the body of the clipping, so do not repeat the headline or begin with a separate headline/dateline. Use one factual paragraph explaining what the current data shows. Aim for roughly 120–220 words. Period flavor is welcome, but clarity comes first: do not invent quotations, causes, troop intentions, battlefield details, or certainty that the packet does not support, and do not favor either faction.

You may receive `previous_model_summary`, your own most recent valid dispatch from an earlier cohort in the current war. Treat it as the previous edition: lead with what has changed since then, note important fronts that remain active or have gone quiet, and avoid repeating the old report word for word. A dispatch from an earlier war is never provided. The current packet is always authoritative; omit an old claim when the new data no longer supports it. If there is no previous summary, write this as the opening edition rather than pretending an earlier report exists.

After the dispatch, select the most active regions whose detailed history would be most useful for predicting strategic-base ownership changes during the next 24 hours. Do not make specific base predictions yet. Return only the requested JSON.
