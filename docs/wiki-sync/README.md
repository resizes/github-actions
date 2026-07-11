# Wiki Sync

Automatically keep an [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)-style docs repository up to date when `main` changes in source repositories.

## What it does

On every push to `main`:

1. Collects the commit diff (or a full repo snapshot on first push)
2. Filters changed files using `.github/wiki-sync.yml`
3. Calls LiteLLM (internal cluster) to decide if the change is wiki-relevant
4. Updates the docs repo under `wiki/` (pages, `index.md`, `log.md`)
5. Commits and pushes directly to the docs repo `main` branch

## Prerequisites

### 1. Docs repository (LLM Wiki layout)

Each customer org needs a docs repo (e.g. `internal-docs`) with this structure:

```
internal-docs/
├── wiki/
│   ├── AGENTS.md
│   ├── index.md
│   ├── log.md
│   ├── overview.md          # optional
│   ├── summaries/
│   ├── concepts/
│   └── entities/
```

Use [`AGENTS.md.template`](./AGENTS.md.template) as the starting schema.

### 2. Resizes-managed GitHub App

Create and install a GitHub App (managed by Resizes) with:

| Permission | Access |
|------------|--------|
| Contents | Read on source repos |
| Contents | Write on docs repos |
| Metadata | Read |

Install the app on:

- **Resizes** org (pilot)
- **RavenwitsSL** org (second rollout)
- Each customer's org

Grant repository access to source repos and the customer's docs repo.

### 3. LiteLLM (ARC cluster)

LiteLLM must be reachable from ARC runners at an internal URL. The action uses the OpenAI-compatible `/v1/chat/completions` endpoint with the **server default model** (no model override in requests).

Set org variables:

| Variable | Example |
|----------|---------|
| `LITELLM_BASE_URL` | `http://litellm.litellm.svc.cluster.local:4000` |
| `WIKI_SYNC_APP_ID` | GitHub App ID |

Set org secrets:

| Secret | Purpose |
|--------|---------|
| `WIKI_SYNC_APP_PRIVATE_KEY` | GitHub App PEM key |
| `LITELLM_API_KEY` | Optional, if LiteLLM requires auth |

### 4. Per-repo config

Add [`.github/wiki-sync.yml`](./example.wiki-sync.yml) to each source repository.

## Caller workflow

### Resizes (pilot)

Copy [example-caller-workflow.resizes.yml](./example-caller-workflow.resizes.yml) to `.github/workflows/wiki-sync.yml` in each Resizes source repo.

### RavenwitsSL (second rollout)

Copy [example-caller-workflow.ravenwits.yml](./example-caller-workflow.ravenwits.yml) to `.github/workflows/wiki-sync.yml` in each RavenwitsSL source repo.

## Validation plan

### Phase 1 — Resizes dry-run

1. Ensure `Resizes/internal-docs` exists with the LLM Wiki layout and `wiki/AGENTS.md`.
2. Pick one low-traffic source repo (e.g. this `github-actions` repo).
3. Add `.github/wiki-sync.yml` and the caller workflow with `dry_run: true` (or use `workflow_dispatch`).
4. Push a docs-relevant change to `main` (e.g. update a README section describing behavior).
5. Review the job summary for proposed wiki updates.
6. Push a non-relevant change (test-only) and confirm the job skips writes.

### Phase 2 — Resizes live push

1. Set `dry_run: false` in the caller workflow (or remove the override).
2. Push a docs-relevant change and verify a commit lands in `Resizes/internal-docs`.
3. Confirm `wiki/log.md` has a new ingest entry and `wiki/index.md` reflects the change.

### Phase 3 — RavenwitsSL

1. Install the GitHub App on RavenwitsSL with access to `internal-docs` and pilot source repos.
2. Repeat dry-run then live-push validation using the RavenwitsSL caller workflow template.

## Inputs (reusable workflow)

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `github_app_id` | yes | — | GitHub App ID |
| `docs_repository_owner` | yes | — | Docs repo owner |
| `docs_repository` | yes | — | Docs repo name |
| `docs_branch` | no | `main` | Branch to push |
| `litellm_base_url` | no | internal K8s URL | LiteLLM base URL |
| `dry_run` | no | `false` | Propose updates only |
| `runner` | no | `actions-runners` | Runner label |

## Relevance criteria

The action updates the wiki when changes affect:

- APIs, contracts, schemas, endpoints
- Infrastructure, deployment, configuration
- Architecture or behavior engineers/customers rely on
- Internal refactors with operational impact

It skips formatting-only, test-only, and no-behavior-change dependency bumps.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| LiteLLM connection error | `LITELLM_BASE_URL` reachable from ARC runners; DNS/service name |
| Permission denied on push | GitHub App has Contents: Write on docs repo |
| Config not found | `.github/wiki-sync.yml` exists in source repo |
| No updates for relevant change | Job summary reason; widen `watch.paths`; inspect LiteLLM response |
| JSON parse error | LiteLLM default model must return valid JSON; check LiteLLM logs |

## Files in this repo

```
wiki-sync/
├── action.yml
└── scripts/
    ├── wiki_sync.py
    └── requirements.txt

.github/workflows/
└── wiki-sync.yml              # reusable workflow

docs/wiki-sync/
├── README.md
├── example.wiki-sync.yml
├── example-caller-workflow.resizes.yml
├── example-caller-workflow.ravenwits.yml
└── AGENTS.md.template
```
