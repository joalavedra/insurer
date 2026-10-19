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

The API reads live collector traces from `INSURER_TRACE_DIR` (default `data/traces`, relative to its working directory); point it at the same directory as `COLLECTOR_DATA_DIR`.

## Navigation and agent registry

`/` redirects to `/monitor`. Monitor lists agents after they have been scored,
with their last-scored time and current Agent Ops policy coverage. The Insurance
tab contains the existing workflow at `/insurance`,
`/insurance/scorecard`, `/insurance/quote`, and
`/insurance/policy/{policy_id}`; the Agents → Scorecard → Quote → Policy
stepper appears only on these routes. The former root-level workflow routes are
removed.

`POST /agent-ops/score` registers or updates the agent manifest and last-scored
time. `GET /agent-ops/agents` supplies Monitor's list and links each agent to
its latest active Agent Ops policy, if any. The policy live-status endpoint can
load the registered manifest when the request omits it, so polling continues
after a reload even without session state.

Clients must forward scorecard bytes unchanged: a JavaScript JSON round-trip drops `.0` from floats and breaks the hash.

Clients must also forward evidence JSON bytes unchanged: JavaScript rounds integers above 2^53, which can alter nanosecond timestamps and scoring or claims results.
