import fs from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';

/**
 * Serve the ORIGINAL RestoPilot landing page and dashboard from this Atoms project.
 *
 * Source files in the repository root frontend/ and landing/ directories are only
 * read, never modified:
 * - build: landing/index.html -> dist/index.html (the site root), dashboard
 *          index.html -> dist/app.html, all assets -> dist/assets/ and
 *          dist/landing-assets/
 * - dev:   the same files are served by middleware, so preview and the saved
 *          build use one path (API calls go to the managed backend via /api).
 *
 * The static host serves existing files at their exact path and falls back to
 * dist/index.html for everything else, so the landing page owns `/` and the
 * dashboard must live at a real file: /app.html. The landing page links there,
 * and the Python app keeps a matching /app.html route for local runs.
 *
 * The only change to the dashboard HTML is loading the API-base shim before
 * app.js and tagging <title> for Atoms overview metadata. The landing HTML is
 * served unmodified.
 */
const adapterDir = path.resolve(__dirname, '..');
const sourceDir = path.resolve(adapterDir, '../../../frontend');
const landingDir = path.resolve(adapterDir, '../../../landing');
const shimPath = path.join(adapterDir, 'restopilot-adapter/api-base.js');
const APP_SCRIPT = '<script src="/assets/app.js" defer></script>';

const ASSETS: Record<string, { file: string; type: string }> = {
  '/assets/i18n.js': { file: path.join(sourceDir, 'i18n.js'), type: 'text/javascript; charset=utf-8' },
  '/assets/app.js': { file: path.join(sourceDir, 'app.js'), type: 'text/javascript; charset=utf-8' },
  '/assets/styles.css': { file: path.join(sourceDir, 'styles.css'), type: 'text/css; charset=utf-8' },
  '/assets/restopilot-api-base.js': { file: shimPath, type: 'text/javascript; charset=utf-8' },
};

const LANDING_ASSETS: Record<string, { file: string; type: string }> = {
  '/landing-assets/styles.css': { file: path.join(landingDir, 'styles.css'), type: 'text/css; charset=utf-8' },
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

function landingHtml(): string {
  const html = fs.readFileSync(path.join(landingDir, 'index.html'), 'utf-8');
  if (!html.includes('href="/app.html"')) {
    throw new Error('RestoPilot landing/index.html no longer links /app.html; update the adapter.');
  }
  if (!html.includes('/landing-assets/styles.css')) {
    throw new Error('RestoPilot landing/index.html no longer references its stylesheet; update the adapter.');
  }
  return html;
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
          res.end(landingHtml());
          return;
        }
        if (url === '/app' || url === '/app.html') {
          res.setHeader('Content-Type', 'text/html; charset=utf-8');
          res.setHeader('Cache-Control', 'no-store');
          res.end(dashboardHtml());
          return;
        }
        const asset = ASSETS[url] || LANDING_ASSETS[url];
        if (asset) {
          res.setHeader('Content-Type', asset.type);
          res.setHeader('Cache-Control', 'no-store');
          res.end(fs.readFileSync(asset.file));
          return;
        }
        next();
      });
    },
    generateBundle() {
      // Register dashboard and landing assets with Rollup as well as writing
      // dist below. Publication must see them in the build output, not only on
      // disk after closeBundle, when a static-asset upload manifest may
      // already exist. app.html is emitted here for the same reason: the
      // dashboard must exist as a real file next to the landing index.html.
      for (const [url, asset] of Object.entries({ ...ASSETS, ...LANDING_ASSETS })) {
        this.emitFile({ type: 'asset', fileName: url.slice(1), source: fs.readFileSync(asset.file) });
      }
      this.emitFile({ type: 'asset', fileName: 'app.html', source: dashboardHtml() });
    },
    closeBundle() {
      if (!outDir) return;
      fs.mkdirSync(path.join(outDir, 'assets'), { recursive: true });
      fs.mkdirSync(path.join(outDir, 'landing-assets'), { recursive: true });
      for (const [url, asset] of Object.entries({ ...ASSETS, ...LANDING_ASSETS })) {
        fs.copyFileSync(asset.file, path.join(outDir, url.slice(1)));
      }
      fs.writeFileSync(path.join(outDir, 'index.html'), landingHtml());
      fs.writeFileSync(path.join(outDir, 'app.html'), dashboardHtml());
    },
  };
}
