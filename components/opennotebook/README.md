# OpenNotebook

[OpenNotebook](https://github.com/lfnovo/open-notebook) – open-source Notebook LM–style app (SurrealDB + FastAPI + Next.js).

## Prerequisites

1. **DNS**: Point `notebook.donovanlowkeen.com` (or your chosen host) at the cluster ingress.
2. **Secrets**: Create an encrypted secret before first use.

## Secrets

The example secret uses placeholder values. For a real deployment:

1. Copy the example:
   ```bash
   cp overlays/prod/opennotebook-secret.example.yaml overlays/prod/opennotebook-secret.enc.yaml
   ```
2. Edit and set:
   - `SURREAL_USER` / `SURREAL_PASSWORD` – SurrealDB credentials
   - `OPENAI_API_KEY` – OpenAI API key
3. Encrypt with SOPS (encrypts in place on save):
   ```bash
   sops overlays/prod/opennotebook-secret.enc.yaml
   ```
4. In `overlays/prod/kustomization.yaml`, replace `opennotebook-secret.example.yaml` with `opennotebook-secret.enc.yaml`.

Optional env vars (same secret, optional keys): `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc. See [OpenNotebook .env.example](https://github.com/lfnovo/open-notebook/blob/main/.env.example).

## Architecture

- **SurrealDB** – separate deployment and PVC for DB
- **OpenNotebook** – single app (API + worker + frontend) talking to SurrealDB via `SURREAL_URL`
- **Ingress** – `/` → frontend (8502), `/api` → API (5055); TLS via cert-manager

## After deploy

- UI: `https://notebook.donovanlowkeen.com`
- `API_URL` is set to that host so the frontend can call the API on the same origin.
