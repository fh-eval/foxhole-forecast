const API_VERSION = "2022-11-28";

export function observationIsStale(observedAt, now, staleAfterMinutes) {
  const observed = Date.parse(observedAt);
  if (!Number.isFinite(observed)) throw new Error("Latest observation has no valid observed_at timestamp");
  return now - observed > staleAfterMinutes * 60_000;
}

export function forecastSlot(now) {
  const value = new Date(now);
  value.setUTCMinutes(0, 0, 0);
  value.setUTCHours(value.getUTCHours() - (value.getUTCHours() % 3));
  return value;
}

export function cacheBustedUrl(url, now) {
  const value = new URL(url);
  value.searchParams.set("watchdog_time", String(now));
  return value.toString();
}

function githubHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "foxhole-forecast-watchdog",
    "X-GitHub-Api-Version": API_VERSION,
  };
}

export function successfulRunSince(runs, notBefore) {
  if (!Number.isFinite(notBefore)) return undefined;
  return runs.find((run) =>
    run.status === "completed"
    && run.conclusion === "success"
    && Date.parse(run.created_at) >= notBefore
  );
}

async function jsonResponse(response, label) {
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`${label} failed with HTTP ${response.status}: ${body.slice(0, 300)}`);
  }
  return response.json();
}

async function dispatchIfIdle(env, workflow, fetchImpl, notBefore) {
  const workflowUrl = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${workflow}`;
  const runsResponse = await fetchImpl(`${workflowUrl}/runs?per_page=10`, {
    headers: githubHeaders(env.GITHUB_TOKEN),
  });
  const runs = await jsonResponse(runsResponse, "Workflow run request");
  const active = (runs.workflow_runs || []).find((run) =>
    ["queued", "in_progress", "waiting", "pending", "requested"].includes(run.status)
  );
  if (active) {
    return { action: "already_active", workflow, run_id: active.id };
  }
  const completed = successfulRunSince(runs.workflow_runs || [], notBefore);
  if (completed) {
    return { action: "recently_completed", workflow, run_id: completed.id };
  }

  const dispatchResponse = await fetchImpl(`${workflowUrl}/dispatches`, {
    method: "POST",
    headers: {
      ...githubHeaders(env.GITHUB_TOKEN),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main" }),
  });
  if (!dispatchResponse.ok) {
    const body = await dispatchResponse.text();
    throw new Error(`Workflow dispatch failed with HTTP ${dispatchResponse.status}: ${body.slice(0, 300)}`);
  }
  return { action: "dispatched", workflow };
}

export async function checkAndDispatch(env, fetchImpl = fetch, now = Date.now()) {
  if (!env.GITHUB_TOKEN) throw new Error("GITHUB_TOKEN secret is not configured");
  const publicHeaders = {
    "Cache-Control": "no-cache",
    Pragma: "no-cache",
    "User-Agent": "foxhole-forecast-watchdog",
  };
  const statusResponse = await fetchImpl(cacheBustedUrl(env.STATUS_DATA_URL, now), {
    headers: publicHeaders,
    cf: { cacheTtl: 0 },
  });
  const status = await jsonResponse(statusResponse, "Pipeline status request");

  const staleAfterMinutes = Number(env.STALE_AFTER_MINUTES || 14);
  const slot = forecastSlot(now);
  const forecastStatus = status.forecast_status || "ready";
  const forecastEligible = forecastStatus === "ready";
  const forecastDue = Date.parse(status.last_forecast_slot || "") !== slot.getTime();
  const observationInSlot = Date.parse(status.observed_at) >= slot.getTime();
  const collectionNeeded = observationIsStale(status.observed_at, now, staleAfterMinutes)
    || (forecastEligible && forecastDue && !observationInSlot);
  const actions = [];

  if (collectionNeeded) {
    actions.push(await dispatchIfIdle(
      env,
      env.COLLECT_WORKFLOW,
      fetchImpl,
      now - staleAfterMinutes * 60_000,
    ));
  }
  if (!forecastEligible && forecastDue) {
    actions.push({ action: "forecast_paused", reason: forecastStatus, workflow: env.FORECAST_WORKFLOW });
  } else if (forecastDue && observationInSlot) {
    actions.push(await dispatchIfIdle(env, env.FORECAST_WORKFLOW, fetchImpl, slot.getTime()));
  } else if (forecastDue) {
    actions.push({ action: "waiting_for_current_slot_observation", workflow: env.FORECAST_WORKFLOW });
  }
  if (!actions.length) actions.push({ action: "fresh" });
  return {
    observed_at: status.observed_at,
    forecast_slot: slot.toISOString(),
    forecast_status: forecastStatus,
    actions,
  };
}

export default {
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(
      checkAndDispatch(env).then((result) => console.log(JSON.stringify(result)))
    );
  },

  async fetch() {
    return Response.json({ service: "foxhole-forecast-watchdog", status: "ok" });
  },
};
