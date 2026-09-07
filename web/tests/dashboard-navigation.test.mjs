import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/index.astro", import.meta.url), "utf8");
const styles = fs.readFileSync(new URL("../src/styles/global.css", import.meta.url), "utf8");
const nav = page.match(/<nav class="section-nav dashboard-nav"[\s\S]*?<\/nav>/)?.[0] || "";
const siteGroup = nav.match(/<div class="dashboard-nav__group dashboard-nav__group--site"[\s\S]*?<\/div>\s*<div class="dashboard-nav__group dashboard-nav__group--dashboard"/)?.[0] || "";
const dashboardGroup = nav.slice(nav.indexOf('class="dashboard-nav__group dashboard-nav__group--dashboard"'));

test("dashboard nav separates same-page anchors from site destinations", () => {
  assert.match(nav, /aria-label="Dashboard navigation"/);
  assert.doesNotMatch(nav, />Explore the site<\/span>/);
  assert.match(siteGroup, /role="group" aria-label="Site navigation"/);
  assert.doesNotMatch(nav, /<details|<summary/);
  assert.match(nav, /aria-label="On this dashboard"/);

  const anchors = [...dashboardGroup.matchAll(/href="(#[^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(anchors, ["#scoreboard", "#war-data", "#rounds", "#briefings", "#probability", "#limitations", "#method", "#top"]);

  const destinations = [...siteGroup.matchAll(/href=\{`\$\{basePath\}([^`]+)`\}/g)].map((match) => match[1]);
  assert.deepEqual(destinations, ["predictions/", "comparisons/", "history/", "prompt-flow/"]);
  assert.match(nav, /class="dashboard-nav__top" href="#top" title="Back to top" aria-label="Back to top"/);
  assert.match(page, /<body id="top">/);
});

test("dashboard nav has a local wrapping treatment and local anchor offset", () => {
  assert.match(page, /<main class="page-width dashboard-page">/);
  assert.match(page, /function settleDashboardHash\(\)/);
  assert.match(page, /renderBriefings\(currentModels\);\s*settleDashboardHash\(\);/);
  const dashboardStyles = styles.slice(styles.indexOf("/* Dashboard navigation"), styles.indexOf("/* ---------- Status strip"));
  assert.match(dashboardStyles, /flex-wrap: wrap/);
  assert.match(dashboardStyles, /overflow: visible/);
  assert.doesNotMatch(dashboardStyles, /overflow-x/);
  assert.match(dashboardStyles, /dashboard-nav__group--site[\s\S]*font-family: var\(--serif\)/);
  assert.match(dashboardStyles, /dashboard-nav__group--site[\s\S]*font-size: 18px/);
  assert.match(dashboardStyles, /dashboard-nav__group--dashboard[\s\S]*border-top: 1px (?:solid|dashed) var\(--line\)/);
  assert.match(dashboardStyles, /dashboard-nav__top[\s\S]*border: 1px solid currentColor/);
  assert.match(dashboardStyles, /\.dashboard-nav \.dashboard-nav__group\s*\{[^}]*display: block;[^}]*text-align: center;/);
  assert.match(styles, /\.dashboard-page \.section-block \{ scroll-margin-top: var\(--dashboard-nav-offset\); \}/);
  assert.match(styles, /\.dashboard-nav \{ position: static; \}/);
});
