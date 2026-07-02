# frbc-tides

Flask app deployed on Render (service name: `frbc-tides`, see `render.yaml`).

## Checking deploy status / logs

Render's API is not directly reachable from Claude Code's sandboxed
environments (network policy blocks `api.render.com`). Instead, use the
`.github/workflows/render-deploy-watch.yml` GitHub Actions workflow:

- It runs automatically on every push to `main`, and can also be triggered
  manually (`workflow_dispatch`).
- It looks up the live Render service, prints recent deploys with status/
  timestamps, and dumps recent build/runtime logs into the job output.
- Requires the `RENDER_API_KEY` repository secret (already configured).

To check on a deploy: trigger or find the latest run of that workflow and
read its job logs, rather than trying to call the Render API directly.
