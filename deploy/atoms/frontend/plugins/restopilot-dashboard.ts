import fs from 'node:fs';
import path from 'node:path';
import type { Plugin } from 'vite';

/**
 * Serve the ORIGINAL RestoPilot landing page and dashboard from this Atoms project.
 *
 * This file is shared verbatim between the GitHub export and the native Atoms
 * workspace, so it resolves sources from the imported application copy under
 * app/deploy/restopilot-integration (the Atoms layout), not from a repository
 * checkout. Nothing in the repository executes it; Atoms' vite build does.
 *
 * Source files are only read, never modified:
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
 * served unmodified. Every mapped source must exist and every copied file must
 * match its source: a published build once shipped stale assets because these
 * checks were missing.
 */
const adapterDir = path.resolve(__dirname, '..');
const integrationDir = path.resolve(adapterDir, '../deploy/restopilot-integration');
const frontendDir = path.join(integrationDir, 'frontend');
const landingDir = path.join(integrationDir, 'landing');
const shimPath = path.join(adapterDir, 'restopilot-adapter/api-base.js');
// Tolerates cache-busted tags like <script src="/assets/app.js?v=20261112a" defer></script>
// ( Frederic's cache-busting broke the exact string match once already).
const APP_SCRIPT_PATTERN = /<script src="\/assets\/app\.js(?:\?[^"]*)?" defer><\/script>/;

const ASSETS: Record<string, { file: string; type: string }> = {
  '/assets/i18n.js': { file: path.join(frontendDir, 'i18n.js'), type: 'text/javascript; charset=utf-8' },
  '/assets/app.js': { file: path.join(frontendDir, 'app.js'), type: 'text/javascript; charset=utf-8' },
  '/assets/styles.css': { file: path.join(frontendDir, 'styles.css'), type: 'text/css; charset=utf-8' },
  '/assets/restopilot-api-base.js': { file: shimPath, type: 'text/javascript; charset=utf-8' },
};

const LANDING_ASSETS: Record<string, { file: string; type: string }> = {
  '/landing-assets/styles.css': { file: path.join(landingDir, 'styles.css'), type: 'text/css; charset=utf-8' },
};

function readSource(file: string): Buffer {
  if (!fs.existsSync(file)) {
    throw new Error(`RestoPilot source is missing: ${file}. Import the application copy under app/deploy/restopilot-integration first.`);
  }
  return fs.readFileSync(file);
}

function dashboardHtml(): string {
  const html = readSource(path.join(frontendDir, 'index.html')).toString('utf-8');
  if (!APP_SCRIPT_PATTERN.test(html)) {
    throw new Error('RestoPilot index.html no longer loads /assets/app.js; update the adapter.');
  }
  return html
    .replace(APP_SCRIPT_PATTERN, `<script src="/assets/restopilot-api-base.js"></script>\n  $&`)
    // The Atoms static host serves the dashboard at the clean URL /app and
    // answers direct /app.html requests with its "not ready" fallback, so
    // every link into the dashboard must use /app (Nov 2026 pilot finding:
    // the landing rendered fine but its login link led to the placeholder).
    .replace(/href="\/app\.html"/g, 'href="/app"')
    .replace(/<title>/, '<title data-mgx-overview="title">');
}

function landingHtml(): string {
  const html = readSource(path.join(landingDir, 'index.html')).toString('utf-8');
  if (!html.includes('href="/app.html"')) {
    throw new Error('RestoPilot landing/index.html no longer links /app.html; update the adapter.');
  }
  if (!html.includes('/landing-assets/styles.css')) {
    throw new Error('RestoPilot landing/index.html no longer references its stylesheet; update the adapter.');
  }
  // Rewrite to the clean /app URL the Atoms static host actually serves.
  return html.replace(/href="\/app\.html"/g, 'href="/app"');
}

function allAssets(): Record<string, { file: string; type: string }> {
  return { ...ASSETS, ...LANDING_ASSETS };
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
        const asset = allAssets()[url];
        if (asset) {
          res.setHeader('Content-Type', asset.type);
          res.setHeader('Cache-Control', 'no-store');
          res.end(readSource(asset.file));
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
      for (const [url, asset] of Object.entries(allAssets())) {
        this.emitFile({ type: 'asset', fileName: url.slice(1), source: readSource(asset.file) });
      }
      this.emitFile({ type: 'asset', fileName: 'app.html', source: Buffer.from(dashboardHtml()) });
    },
    closeBundle() {
      if (!outDir) return;
      for (const [url, asset] of Object.entries(allAssets())) {
        const target = path.join(outDir, url.slice(1));
        fs.mkdirSync(path.dirname(target), { recursive: true });
        const source = readSource(asset.file);
        fs.writeFileSync(target, source);
        if (!fs.readFileSync(target).equals(source)) {
          throw new Error(`RestoPilot build output does not match its source: ${url}`);
        }
      }
      const indexTarget = path.join(outDir, 'index.html');
      fs.writeFileSync(indexTarget, landingHtml());
      if (!fs.readFileSync(indexTarget).toString('utf-8').includes('/landing-assets/styles.css')) {
        throw new Error('RestoPilot build output lost the landing index.html.');
      }
      const dashboard = dashboardHtml();
      const appTarget = path.join(outDir, 'app.html');
      fs.writeFileSync(appTarget, dashboard);
      if (!APP_SCRIPT_PATTERN.test(fs.readFileSync(appTarget).toString('utf-8'))) {
        throw new Error('RestoPilot build output lost the dashboard app.html.');
      }
      // The Atoms static host serves the dashboard at the clean URL /app and
      // 404s direct /app.html requests; publish the dashboard under both so
      // the landing link and any bookmarked legacy URL both work.
      const cleanTarget = path.join(outDir, 'app');
      fs.writeFileSync(cleanTarget, dashboard);
      if (!APP_SCRIPT_PATTERN.test(fs.readFileSync(cleanTarget).toString('utf-8'))) {
        throw new Error('RestoPilot build output lost the dashboard /app.');
      }
    },
  };
}
