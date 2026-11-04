import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';

export const toolsDir = path.dirname(fileURLToPath(import.meta.url));
export const localDir = path.join(toolsDir, '.local');
process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(localDir, 'browsers');
export const { chromium } = await import('playwright');

export async function openAtoms() {
  await mkdir(localDir, { recursive: true });
  const context = await chromium.launchPersistentContext(
    path.join(localDir, 'atoms-profile'),
    { headless: false, viewport: null },
  );
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto('https://atoms.dev/dashboard', { waitUntil: 'domcontentloaded' });
  return { context, page };
}
