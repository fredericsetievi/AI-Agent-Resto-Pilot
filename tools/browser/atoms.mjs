import { openAtoms } from './runtime.mjs';
import repl from 'node:repl';

const { context, page } = await openAtoms();
console.log(`Atoms browser opened: ${await page.title()}`);
console.log('Sign in to Atoms in this window. This separate browser profile retains your login locally.');
console.log('Close the browser window or press Ctrl+C to finish.');
let terminal;
if (process.argv.includes('--interactive')) {
  terminal = repl.start({ prompt: 'atoms> ' });
  terminal.context.page = page;
  terminal.context.context = context;
  console.log('Playwright page and context are available here; top-level await is supported.');
  terminal.on('exit', () => { void close(); });
}
let closing = false;
async function close() {
  if (closing) return;
  closing = true;
  await context.close();
}
process.on('SIGINT', close);
process.on('SIGTERM', close);
await new Promise(resolve => context.on('close', resolve));
terminal?.close();
