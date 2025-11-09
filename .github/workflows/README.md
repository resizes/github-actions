# Renovate
## What is Renovate
Renovate is an open-source tool (by Mend) that **automates dependency updates** across basically any language or ecosystem. It supports npm, pip, Terraform, Docker, Maven, Go modules, and a bunch more.

At a high level, Renovate:
- Scans your repo for dependencies.
- Checks if newer versions are available.
- Creates pull requests with the updates.

## How to use Renovate Action
### **Create your own ``renovate.json``:**
You can use this as a template and adapt it to your needs later on. Add it to your repo's root level:
```json
{
    "$schema": "https://docs.renovatebot.com/renovate-schema.json",
    "extends": [
        "config:recommended",
        ":dependencyDashboard",
        ":semanticCommits",
        ":automergeBranch"
    ],
    "timezone": "Europe/London",
    "schedule": [
        "at any time"
    ],
    "labels": [
        "renovate",
        "terraform"
    ],
    "assignees": [],
    "reviewers": [],
    "prConcurrentLimit": 0,
    "prHourlyLimit": 0,
    "rebaseWhen": "conflicted",
    "lockFileMaintenance": {
        "enabled": false
    },
    "digest": {
        "automerge": true
    },

    "packageRules": [
        {
            "description": "Terraform providers and modules",
            "matchManagers": [
                "terraform"
            ],
            "commitMessageTopic": "Terraform {{depName}}",
            "pinDigests": false
        },{
            "matchManagers": ["nvm", "node-version", "asdf"],
            "groupName": "Node.js version",
            "groupSlug": "node-version",
            "semanticCommitType": "chore",
            "semanticCommitScope": "node",
            "commitMessageTopic": "Node.js version",
            "commitMessageExtra": "update Node.js runtime version"
          },
          {
            "matchManagers": ["npm"],
            "groupName": "npm dependencies",
            "semanticCommitType": "chore",
            "semanticCommitScope": "deps"
          }
    ],
  
    "npm": {
        "enabled": true
    },
  
    "nvm": {
        "enabled": true
    },
  
    "asdf": {
        "enabled": true
    }

}
```

### **Invoke our reusable workflow from your repo:** You can use our reusable workflow from our [GitHub Actions repo](https://github.com/resizes/github-actions/blob/main/.github/workflows/renovate.yml):
```yaml
name: Renovate

on:
  schedule:
  # Run every Monday at 5:00 AM UTC
  - cron: '0 5 * * 1'
  workflow_dispatch:
    inputs:
      log_level:
        description: 'Log level'
        required: false
        default: 'info'
        type: choice
        options:
        - info
        - debug
        - trace
      dry_run:
        description: 'Dry run (no PRs will be created)'
        required: false
        default: false
        type: boolean
      force_refresh:
        description: 'Force refresh all dependencies'
        required: false
        default: false
        type: boolean

permissions:
  contents: write # To create branches and commits
  pull-requests: write # To create and update pull requests
  issues: write # For dependency dashboard (if enabled)
  checks: read # To read check status
  statuses: read # To read commit statuses
  actions: read # To read workflow runs
  security-events: read # To read security events

jobs:
  renovate:
    uses: resizes/github-actions/.github/workflows/renovate.yml@v1
    with:
      log_level: ${{ inputs.log_level }}
      dry_run: ${{ inputs.dry_run }}
      force_refresh: ${{ inputs.force_refresh }}
      runner: 'ubuntu-latest'
      github_app_id: ${{ secrets.RENOVATE_APP_ID }}
      owner: 'Resizes'
      repositories: |
        <Your repository>
    secrets:
      github_app_private_key: ${{ secrets.RENOVATE_APP_PRIVATE_KEY }}
```