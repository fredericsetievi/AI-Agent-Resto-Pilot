import { defineConfig } from '@playwright/test';

export default defineConfig({
  // Plugin unit tests use node:test; keep them out of generated-app E2E runs.
  testIgnore: ['**/plugins/*.test.ts'],
});
