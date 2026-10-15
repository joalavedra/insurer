# Agent Ops Cover UI

The UI runs as a TanStack Start app and calls the Python FastAPI backend through server functions. From the repository root, start the API:

```bash
insurer serve --db /tmp/agent-ops.db
```

In another terminal, install and start the web app:

```bash
cd web
npm ci
npm run dev
```

The API listens on `http://127.0.0.1:8000` and the web app on `http://localhost:3000`. To use a different API URL, set `INSURER_API_URL` in the web server environment before starting the dev server.

Clients must forward scorecard bytes unchanged: a JavaScript JSON round-trip drops `.0` from floats and breaks the hash.
