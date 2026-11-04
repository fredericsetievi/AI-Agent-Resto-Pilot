import assert from 'node:assert/strict';
import { chromium } from './runtime.mjs';

const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.setContent('<label>Prompt <input aria-label="Prompt"></label><button>Send</button><p role="status"></p><script>document.querySelector("button").onclick = () => {document.querySelector("p").textContent = document.querySelector("input").value}</script>');
  await page.getByRole('textbox', { name: 'Prompt' }).fill('RestoPilot browser ready');
  await page.getByRole('button', { name: 'Send' }).click();
  assert.equal(await page.getByRole('status').textContent(), 'RestoPilot browser ready');
  console.log(`Playwright passed: Chromium ${browser.version()}, prompt entry, click, and result verification.`);
} finally {
  await browser.close();
}
