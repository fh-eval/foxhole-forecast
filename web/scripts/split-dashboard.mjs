import { readFile, writeFile } from "node:fs/promises";

const sourceUrl = new URL("../public/data/dashboard.json", import.meta.url);
const dashboard = JSON.parse(await readFile(sourceUrl, "utf8"));
const outputUrl = (name) => new URL(`../public/data/${name}`, import.meta.url);
const writeData = (name, value) => writeFile(outputUrl(name), JSON.stringify(value));

function latestRoundGroups(rounds, protocol, limit = 3) {
  const selected = [];
  const groups = new Set();
  for (const round of rounds) {
    if (round.protocol !== protocol) continue;
    const key = `${round.war_id || "unknown"}:${round.round_slot || round.cutoff}`;
    if (!groups.has(key)) {
      if (groups.size >= limit) continue;
      groups.add(key);
    }
    selected.push(round);
  }
  return selected;
}

const main = {
  ...dashboard,
  models: (dashboard.models || []).map(({ history, latest_all_time, ...model }) => model),
  rounds: latestRoundGroups(
    dashboard.rounds || [],
    dashboard.methodology?.current_protocol,
  ),
  base_forecasts: [],
};
const roundHistory = {
  schema_version: dashboard.schema_version,
  generated_at: dashboard.generated_at,
  rounds: dashboard.rounds || [],
};
const summaryHistory = {
  schema_version: dashboard.schema_version,
  generated_at: dashboard.generated_at,
  models: (dashboard.models || [])
    .filter((model) => model.history?.length)
    .map((model) => ({
      series_id: model.series_id,
      label: model.label,
      history: model.history,
    })),
};

await Promise.all([
  writeData("dashboard-main.json", main),
  writeData("round-history.json", roundHistory),
  writeData("summary-history.json", summaryHistory),
]);
