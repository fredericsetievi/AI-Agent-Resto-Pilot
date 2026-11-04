import fs from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';

/**
 * Serve the ORIGINAL RestoPilot dashboard from this Atoms project.
 *
 * Source files in the repository root frontend/ directory are only read, never modified:
 * - build: index.html -> dist/index.html; app.js, styles.css -> dist/assets/
 * - dev:   the same files are served by middleware, so preview and the saved
 *          build use one path (API calls go to the managed backend via /api).
 *
 * The only change to the served HTML is loading the API-base shim before
 * app.js and tagging <title> for Atoms overview metadata.
 */
const adapterDir = path.resolve(__dirname, '..');
const sourceDir = path.resolve(adapterDir, '../../../frontend');
const shimPath = path.join(adapterDir, 'restopilot-adapter/api-base.js');
const APP_SCRIPT = '<script src="/assets/app.js" defer></script>';

const ASSETS: Record<string, { file: string; type: string }> = {
  '/assets/app.js': { file: path.join(sourceDir, 'app.js'), type: 'text/javascript; charset=utf-8' },
  '/assets/styles.css': { file: path.join(sourceDir, 'styles.css'), type: 'text/css; charset=utf-8' },
  '/assets/restopilot-api-base.js': { file: shimPath, type: 'text/javascript; charset=utf-8' },
};

function dashboardHtml(): string {
  const html = fs.readFileSync(path.join(sourceDir, 'index.html'), 'utf-8');
  if (!html.includes(APP_SCRIPT)) {
    throw new Error('RestoPilot index.html no longer loads /assets/app.js; update the adapter.');
  }
  return html
    .replace(APP_SCRIPT, `<script src="/assets/restopilot-api-base.js"></script>\n  ${APP_SCRIPT}`)
    .replace(/<title>/, '<title data-mgx-overview="title">');
}

export function restopilotDashboard(): Plugin {
  let outDir = '';

  return {
    name: 'restopilot-dashboard',
    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir);
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url || '/').split('?')[0];
        if (req.method !== 'GET' && req.method !== 'HEAD') return next();
        if (url === '/' || url === '/index.html') {
          res.setHeader('Content-Type', 'text/html; charset=utf-8');
          res.setHeader('Cache-Control', 'no-store');
          res.end(dashboardHtml());
          return;
        }
        const asset = ASSETS[url];
        if (asset) {
          res.setHeader('Content-Type', asset.type);
          res.setHeader('Cache-Control', 'no-store');
          res.end(fs.readFileSync(asset.file));
          return;
        }
        next();
      });
    },
    closeBundle() {
      if (!outDir) return;
      const assetsDir = path.join(outDir, 'assets');
      fs.mkdirSync(assetsDir, { recursive: true });
      for (const [url, asset] of Object.entries(ASSETS)) {
        fs.copyFileSync(asset.file, path.join(outDir, url.slice(1)));
      }
      fs.writeFileSync(path.join(outDir, 'index.html'), dashboardHtml());
    },
  };
}
