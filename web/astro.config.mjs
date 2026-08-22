import { defineConfig } from "astro/config";

const repository = process.env.GITHUB_REPOSITORY?.split("/")[1];
const isUserSite = repository?.endsWith(".github.io");

export default defineConfig({
  site: process.env.SITE_URL || "https://example.github.io",
  base: isUserSite || !repository ? "/" : `/${repository}`,
  output: "static",
});

