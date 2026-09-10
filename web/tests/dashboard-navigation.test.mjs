import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/index.astro", import.meta.url), "utf8");
const component = fs.readFileSync(new URL("../src/components/DashboardNav.astro", import.meta.url), "utf8");
const dashboardScript = fs.readFileSync(new URL("../src/scripts/dashboard.ts", import.meta.url), "utf8");
const navigationStyles = fs.readFileSync(new URL("../src/styles/05-masthead-nav.css", import.meta.url), "utf8");
const responsiveStyles = fs.readFileSync(new URL("../src/styles/25-responsive.css", import.meta.url), "utf8");
const siteGroup = component.match(/<div class="dashboard-nav__group dashboard-nav__group--site"[\s\S]*?<\/div>\s*<\/div>/)?.[0] || "";
const dashboardGroup = component.match(/<div class="dashboard-nav__group dashboard-nav__group--dashboard"[\s\S]*?<\/div>\s*<\/div>/)?.[0] || "";

test("dashboard nav separates same-page anchors from site destinations", () => {
  assert.match(component, /<nav class="section-nav dashboard-nav" aria-label="Dashboard navigation">/);
  assert.doesNotMatch(component, />Explore the site<\/span>/);
  assert.match(siteGroup, /role="group" aria-label="Site navigation"/);
  assert.doesNotMatch(component, /<details|<summary/);
  assert.match(dashboardGroup, /role="group" aria-label="On this dashboard"/);

  const anchors = [...dashboardGroup.matchAll(/href="(#[^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(anchors, ["#scoreboard", "#war-data", "#rounds", "#briefings", "#probability", "#limitations", "#method", "#top"]);

  const destinations = [...siteGroup.matchAll(/href=\{`\$\{basePath\}([^`]+)`\}/g)].map((match) => match[1]);
  assert.deepEqual(destinations, ["predictions/", "comparisons/", "history/", "prompt-flow/"]);
  assert.match(component, /class="dashboard-nav__top" href="#top" title="Back to top" aria-label="Back to top"/);
  assert.match(page, /import DashboardNav from "\.\.\/components\/DashboardNav\.astro";/);
  assert.match(page, /<DashboardNav basePath=\{basePath\} \/>/);
  assert.match(page, /<body id="top">/);
});

test("dashboard nav has a local wrapping treatment and local anchor offset", () => {
  assert.match(page, /<main class="page-width dashboard-page">/);
  assert.match(dashboardScript, /function settleDashboardHash\(\)/);
  assert.match(dashboardScript, /renderBriefings\(currentModels\);\s*settleDashboardHash\(\);/);
  assert.match(dashboardScript, /getComputedStyle\(nav\)\.position === "sticky"/);
  const dashboardStyles = navigationStyles.slice(navigationStyles.indexOf("/* Dashboard navigation"));
  assert.match(dashboardStyles, /flex-wrap: wrap/);
  assert.match(dashboardStyles, /overflow: visible/);
  assert.doesNotMatch(dashboardStyles, /overflow-x/);
  assert.match(dashboardStyles, /dashboard-nav__group--site[\s\S]*font-family: var\(--serif\)/);
  assert.match(dashboardStyles, /dashboard-nav__group--site[\s\S]*font-size: 18px/);
  assert.match(dashboardStyles, /dashboard-nav__group--dashboard[\s\S]*border-top: 1px (?:solid|dashed) var\(--line\)/);
  assert.match(dashboardStyles, /dashboard-nav__top[\s\S]*border: 1px solid currentColor/);
  assert.match(dashboardStyles, /\.dashboard-nav \.dashboard-nav__group\s*\{[^}]*display: block;[^}]*text-align: center;/);
  assert.match(navigationStyles, /\.dashboard-page \.section-block \{ scroll-margin-top: var\(--dashboard-nav-offset\); \}/);
  assert.match(responsiveStyles, /\.dashboard-nav \{ position: static; \}/);
});
