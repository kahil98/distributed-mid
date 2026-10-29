# Strata React console

React and Vite user interface for the distributed storage gateway. Start the API stack from the repository root first, then:

```powershell
cd frontend
Copy-Item .env.example .env.local
npm install
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`). Sign in using the gateway's configured account; local Compose defaults are `exam-user` / `change-me`.

The console supports bucket creation, object upload/download/delete, Elasticsearch metadata search, cluster/node health, per-node fragment distribution, degraded objects, and repair job progress. The gateway allows the Vite dev origin through CORS. Search/indexing requires Elasticsearch from `compose.storage.yaml`.
