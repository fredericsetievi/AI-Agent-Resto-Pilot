import path from 'node:path';
import { fileURLToPath } from 'node:url';
import repl from 'node:repl';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const dir = path.dirname(fileURLToPath(import.meta.url));
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(dir, 'node_modules/@playwright/mcp/cli.js'), '--extension',
    '--output-dir', path.join(dir, '.local/mcp')],
  stderr: 'inherit',
});
const client = new Client({ name: 'RestoPilot', version: '1.0.0' });
await client.connect(transport);
const { tools } = await client.listTools();
console.log(`Playwright MCP ready (${tools.length} tools). Install the official Playwright Chrome extension before connecting.`);
console.log('Run await call("browser_tabs", {action: "list"}) to request a connection, then approve the Atoms tab in Chrome.');
const terminal = repl.start({ prompt: 'chrome> ' });
terminal.context.client = client;
terminal.context.tools = tools;
terminal.context.call = async (name, args = {}) => {
  const result = await client.callTool({ name, arguments: args });
  for (const item of result.content ?? []) {
    if (item.type === 'text') console.log(item.text);
  }
  return { isError: result.isError ?? false };
};
terminal.on('exit', async () => { await client.close(); });
