# RestoPilot browser tools

Separate Playwright tooling for working in Atoms. The RestoPilot app remains Python-based.

## Use your signed-in Chrome session (recommended for Atoms)

Google may reject sign-in from the separate automated Chromium browser. Use Microsoft's official extension to connect to the Atoms session in your ordinary Chrome profile instead:

1. Install the [official Playwright extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm) in your normal Chrome profile.
2. Run `npm run atoms:chrome` from this directory.
3. In its interactive terminal, run `await call('browser_tabs', { action: 'list' })`.
4. Approve the connection and select the Atoms tab when the extension asks. Keep other tabs outside the connection's tab group.
5. Run `await call('browser_snapshot')` to verify access. The `tools` variable contains the available MCP tools and their input schemas.

This uses per-connection approval and does not save a token that skips the approval dialog. Exit the terminal with `.exit` to disconnect.

Reference: [Microsoft's extension setup](https://github.com/microsoft/playwright/blob/main/packages/extension/README.md).

## Separate browser (for sites that permit automated sign-in)

From this directory, install dependencies and Chromium:

```sh
npm ci
PLAYWRIGHT_BROWSERS_PATH=.local/browsers npx playwright install chromium
npm run check
npm run atoms
```

Sign in to Atoms in the browser opened by `npm run atoms`. It uses a dedicated persistent profile, so it does not share your ordinary Chrome login. The profile and browser downloads live under `.local/`, which Git ignores. Keep that directory private; it contains login session data.

For agent automation from Node, import `openAtoms` from `runtime.mjs` and use the returned `page` and `context` with Playwright. Only one process can use this profile at a time, so close the launcher browser before opening it from another process. No remote debugging port is exposed.

Alternatively, run `npm run atoms -- --interactive` for a terminal with `page` and `context` available. For example, `await page.title()` reads the current title. This also works when an agent's embedded JavaScript runtime cannot load ordinary Node packages.

Reference: [Playwright library documentation](https://playwright.dev/docs/library).
