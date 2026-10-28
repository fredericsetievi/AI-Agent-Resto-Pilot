/**
 * RestoPilot adapter — catch-all entry.
 *
 * The real dashboard is the imported app's own `frontend/index.html`, served by
 * the FastAPI backend and proxied by Vite for `/`, `/assets`, `/v1`, `/health`
 * and `/webhooks`. This React shell exists only so the Atoms web-project tooling
 * has a valid entrypoint; it must never render a placeholder UI of its own.
 *
 * Any path that reaches here is not part of the app's route surface, so we send
 * the visitor to `/` where the original dashboard is served.
 */
const Index = () => {
  if (typeof window !== 'undefined' && window.location.pathname !== '/') {
    window.location.replace('/');
  }

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        fontFamily: 'system-ui, sans-serif',
        color: '#183f35',
        background: '#f7f5f0',
      }}
    >
      Opening RestoPilot…
    </div>
  );
};

export default Index;
