#!/usr/bin/env python3
"""Sync source repository changes into an LLM-maintained wiki docs repo."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

MAX_DIFF_BYTES = 50_000
WIKI_PAGE_DIRS = ("concepts", "entities", "sources", "syntheses", "comparisons")
TEXT_EXTENSIONS = {
    ".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
    ".env", ".sh", ".bash", ".py", ".js", ".ts", ".tsx", ".jsx", ".go",
    ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".sql", ".hcl", ".tf",
    ".tfvars", ".dockerfile", ".graphql", ".proto", ".xml", ".html", ".css",
}


@dataclass
class ChangeSet:
    before_sha: str
    after_sha: str
    is_initial: bool
    files: list[dict[str, Any]]


@dataclass
class WikiUpdate:
    path: str
    action: str
    content: str


@dataclass
class AnalysisResult:
    relevant: bool
    reason: str
    updates: list[WikiUpdate]
    log_entry: str


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Wiki sync config not found: {config_path}")
    with config_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def is_probably_text(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return True
    name = Path(path).name.lower()
    return name in {"dockerfile", "makefile", "readme", "license", "procfile"}


def read_text_file(path: Path, limit: int | None = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in data[:8192]:
        return "[binary file omitted]"
    text = data.decode("utf-8", errors="replace")
    if limit is not None and len(text) > limit:
        return text[:limit] + "\n...[truncated]"
    return text


def collect_initial_changes(source_dir: Path, max_files: int) -> list[dict[str, Any]]:
    result = run(["git", "ls-files"], cwd=source_dir)
    files: list[dict[str, Any]] = []
    for rel_path in result.stdout.splitlines():
        if not rel_path:
            continue
        abs_path = source_dir / rel_path
        if not abs_path.is_file():
            continue
        files.append(
            {
                "path": rel_path,
                "status": "snapshot",
                "diff": read_text_file(abs_path, MAX_DIFF_BYTES) if is_probably_text(rel_path) else "[binary file omitted]",
            }
        )
        if len(files) >= max_files:
            break
    return files


def collect_commit_changes(source_dir: Path, before_sha: str, after_sha: str, max_files: int) -> ChangeSet:
    is_initial = before_sha == "0000000000000000000000000000000000000000"
    if is_initial:
        return ChangeSet(
            before_sha=before_sha,
            after_sha=after_sha,
            is_initial=True,
            files=collect_initial_changes(source_dir, max_files),
        )

    result = run(
        ["git", "diff", "--name-status", before_sha, after_sha],
        cwd=source_dir,
    )
    files: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        rel_path = parts[-1]
        diff = ""
        if status != "D" and is_probably_text(rel_path):
            diff_result = run(
                ["git", "diff", before_sha, after_sha, "--", rel_path],
                cwd=source_dir,
                check=False,
            )
            diff = diff_result.stdout[:MAX_DIFF_BYTES]
            if len(diff_result.stdout) > MAX_DIFF_BYTES:
                diff += "\n...[truncated]"
        files.append({"path": rel_path, "status": status, "diff": diff})
        if len(files) >= max_files:
            break

    return ChangeSet(before_sha=before_sha, after_sha=after_sha, is_initial=False, files=files)


def filter_changes(files: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    watch_cfg = config.get("watch", {})
    watch_paths = watch_cfg.get("paths", ["**/*"])
    ignore_paths = watch_cfg.get("ignore", [])

    filtered: list[dict[str, Any]] = []
    for item in files:
        path = item["path"]
        if watch_paths and not matches_any(path, watch_paths):
            continue
        if ignore_paths and matches_any(path, ignore_paths):
            continue
        filtered.append(item)
    return filtered


def wiki_context(docs_dir: Path) -> dict[str, str]:
    context: dict[str, str] = {}

    for candidate in ("AGENTS.md", "CLAUDE.md"):
        path = docs_dir / candidate
        if path.exists():
            context["schema"] = read_text_file(path, 20_000)
            break

    index_path = docs_dir / "index.md"
    if index_path.exists():
        context["index"] = read_text_file(index_path, 20_000)

    log_path = docs_dir / "log.md"
    if log_path.exists():
        context["log_tail"] = "\n".join(read_text_file(log_path, 10_000).splitlines()[-40:])

    overview_path = docs_dir / "overview.md"
    if overview_path.exists():
        context["overview"] = read_text_file(overview_path, 10_000)

    listing: list[str] = []
    for wiki_dir_name in WIKI_PAGE_DIRS:
        wiki_sub = docs_dir / wiki_dir_name
        if not wiki_sub.exists():
            continue
        for path in sorted(wiki_sub.rglob("*.md")):
            rel = path.relative_to(docs_dir).as_posix()
            listing.append(rel)
            if len(listing) >= 200:
                break
        if len(listing) >= 200:
            break
    context["page_list"] = "\n".join(listing)
    return context


def build_prompt(
    *,
    source_repo: str,
    change_set: ChangeSet,
    filtered_files: list[dict[str, Any]],
    wiki_ctx: dict[str, str],
    source_readme: str,
) -> str:
    files_blob = json.dumps(filtered_files, indent=2)
    wiki_blob = json.dumps(wiki_ctx, indent=2)
    return textwrap.dedent(
        f"""
        You maintain the Resizes internal-technical-docs LLM wiki for engineers.

        Source repository: {source_repo}
        Commit range: {change_set.before_sha} -> {change_set.after_sha}
        Initial snapshot: {change_set.is_initial}

        Relevance bar — mark relevant=true ONLY when changes affect:
        - Public or internal APIs, contracts, endpoints, schemas
        - Infrastructure, deployment, configuration, environment variables
        - Architecture or behavior that engineers or customers rely on
        - Internal refactors that change how systems work, not cosmetic-only edits

        Ignore: formatting-only changes, test-only changes, dependency bumps with no behavior change,
        comments-only edits, CI boilerplate, unless they change operational behavior.

        Wiki pattern (strict — follow AGENTS.md in the docs repo):
        - Wiki pages live at repo root under `concepts/`, `entities/`, `sources/`, `syntheses/`, `comparisons/`
        - Special files at repo root: `index.md`, `log.md`, optional `overview.md`
        - Raw sources under `raw/` are immutable — never modify them
        - Every ingest must update cross-referenced pages, `index.md`, and append to `log.md`
        - Use Obsidian-style [[wikilinks]] between pages
        - Pages must include YAML frontmatter per AGENTS.md
        - Prefer updating existing pages over creating duplicates
        - New page when the topic is a distinct entity/concept; edit in place for attribute updates

        Source README (context):
        ```
        {source_readme[:8000]}
        ```

        Changed files (path, status, diff/content):
        ```json
        {files_blob}
        ```

        Current wiki context:
        ```json
        {wiki_blob}
        ```

        Return ONLY valid JSON with this schema:
        {{
          "relevant": boolean,
          "reason": "string",
          "log_entry": "markdown line without leading ##, e.g. [2026-07-11] ingest | repo@sha | summary",
          "updates": [
            {{
              "path": "concepts/example.md",
              "action": "create|update|append",
              "content": "full file content for create/update, or append block for append"
            }}
          ]
        }}

        Rules for updates:
        - Paths must be relative to docs repo root (no `wiki/` prefix)
        - Include all touched wiki pages in `updates`
        - Always include updated `index.md` and an append to `log.md`
        - For `log.md`, use action=append
        - If not relevant, return {{"relevant": false, "reason": "...", "log_entry": "", "updates": []}}
        """
    ).strip()


DEFAULT_LITELLM_MODEL = "ollama_chat/glm-5:cloud"


def resolve_litellm_model(*, base_url: str, api_key: str, configured_model: str = "") -> str:
    if configured_model.strip():
        return configured_model.strip()
    return DEFAULT_LITELLM_MODEL


def call_litellm(*, base_url: str, api_key: str, prompt: str, model: str = "", timeout: int = 300) -> str:
    model_name = resolve_litellm_model(base_url=base_url, api_key=api_key, configured_model=model)
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a disciplined wiki maintainer. Follow the LLM wiki pattern exactly. "
                    "Respond with JSON only, no markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LiteLLM request failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LiteLLM request failed: {exc}") from exc

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LiteLLM response: {json.dumps(body)[:2000]}") from exc


def extract_json(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1)
    else:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def parse_analysis(content: str) -> AnalysisResult:
    payload = extract_json(content)
    updates = [
        WikiUpdate(
            path=item["path"],
            action=item.get("action", "update"),
            content=item.get("content", ""),
        )
        for item in payload.get("updates", [])
        if item.get("path")
    ]
    return AnalysisResult(
        relevant=bool(payload.get("relevant")),
        reason=str(payload.get("reason", "")),
        updates=updates,
        log_entry=str(payload.get("log_entry", "")),
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def apply_updates(docs_dir: Path, updates: list[WikiUpdate], *, log_entry: str) -> list[str]:
    changed: list[str] = []
    log_path = docs_dir / "log.md"

    for update in updates:
        target = docs_dir / update.path
        if update.action == "append":
            ensure_parent(target)
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
            block = update.content
            if existing and not existing.endswith("\n"):
                block = "\n" + block
            target.write_text(existing + block, encoding="utf-8")
        else:
            ensure_parent(target)
            target.write_text(update.content, encoding="utf-8")
        changed.append(update.path)

    if log_entry and "log.md" not in changed:
        ensure_parent(log_path)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = log_entry.strip()
        if not entry.startswith("##"):
            entry = f"## {entry}"
        if not entry.startswith("## ["):
            entry = re.sub(r"^##\s*", f"## [{timestamp}] ", entry)
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Wiki Log\n"
        if not existing.endswith("\n"):
            existing += "\n"
        log_path.write_text(existing + entry + "\n", encoding="utf-8")
        changed.append("log.md")

    return sorted(set(changed))


def write_step_summary(message: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def git_commit_and_push(
    docs_dir: Path,
    *,
    token: str,
    branch: str,
    message: str,
    owner: str = "",
    repo: str = "",
) -> None:
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=docs_dir)
    run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=docs_dir)
    run(["git", "add", "-A"], cwd=docs_dir)
    status = run(["git", "status", "--porcelain"], cwd=docs_dir)
    if not status.stdout.strip():
        return

    run(["git", "commit", "-m", message], cwd=docs_dir)

    if owner and repo:
        auth_remote = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    else:
        remote = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=docs_dir, text=True).strip()
        secure_remote = re.sub(r"^https://[^/]+@", "https://", remote)
        auth_remote = secure_remote.replace("https://", f"https://x-access-token:{token}@")

    run(["git", "remote", "set-url", "origin", auth_remote], cwd=docs_dir)
    pull = run(["git", "pull", "--rebase", "origin", branch], cwd=docs_dir, check=False)
    if pull.returncode != 0:
        raise RuntimeError(pull.stderr or pull.stdout or "git pull --rebase failed")
    push = run(["git", "push", "origin", f"HEAD:{branch}"], cwd=docs_dir, check=False)
    if push.returncode != 0:
        raise RuntimeError(push.stderr or push.stdout or "git push failed")


def dispatch_wiki_sync(
    *,
    token: str,
    owner: str,
    repo: str,
    source_repo: str,
    after_sha: str,
    analysis: AnalysisResult,
) -> None:
    url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
    payload = {
        "event_type": "wiki-sync",
        "client_payload": {
            "source_repo": source_repo,
            "after_sha": after_sha,
            "reason": analysis.reason,
            "log_entry": analysis.log_entry,
            "updates": [
                {"path": u.path, "action": u.action, "content": u.content}
                for u in analysis.updates
            ],
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status not in (200, 204):
                raise RuntimeError(f"repository_dispatch failed with status {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"repository_dispatch failed ({exc.code}): {detail}") from exc


def analysis_to_result(payload: dict[str, Any]) -> AnalysisResult:
    updates = [
        WikiUpdate(
            path=item["path"],
            action=item.get("action", "update"),
            content=item.get("content", ""),
        )
        for item in payload.get("updates", [])
        if item.get("path")
    ]
    return AnalysisResult(
        relevant=bool(payload.get("relevant", True)),
        reason=str(payload.get("reason", "")),
        updates=updates,
        log_entry=str(payload.get("log_entry", "")),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync repository changes into LLM wiki docs.")
    parser.add_argument("--source-dir", default=".")
    parser.add_argument("--docs-dir", default=".")
    parser.add_argument("--config", default=".github/wiki-sync.yml")
    parser.add_argument("--source-repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--before-sha", default=os.environ.get("GITHUB_EVENT_BEFORE", "0000000000000000000000000000000000000000"))
    parser.add_argument("--after-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--litellm-base-url", default=os.environ.get("LITELLM_BASE_URL", "https://litellm.internal.resiz.es"))
    parser.add_argument("--litellm-api-key", default=os.environ.get("LITELLM_API_KEY", ""))
    parser.add_argument("--litellm-model", default=os.environ.get("LITELLM_MODEL", ""))
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", os.environ.get("DOCS_GITHUB_TOKEN", "")))
    parser.add_argument("--dispatch-owner", default="")
    parser.add_argument("--dispatch-repo", default="")
    parser.add_argument("--docs-branch", default="main")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply-local", action="store_true")
    parser.add_argument("--apply-from-json", default="")
    return parser.parse_args()


def apply_from_payload(args: argparse.Namespace) -> int:
    docs_dir = Path(args.docs_dir).resolve()
    payload = json.loads(args.apply_from_json)
    source_repo = str(payload.get("source_repo", "unknown"))
    after_sha = str(payload.get("after_sha", "unknown"))
    analysis = analysis_to_result(payload)

    if not analysis.updates:
        print("No wiki updates in payload.")
        return 0

    changed_paths = apply_updates(docs_dir, analysis.updates, log_entry=analysis.log_entry)
    commit_message = (
        f"docs(sync): update wiki from {source_repo}@{after_sha[:7]}\n\n"
        f"[skip ci]\n\n"
        f"Updated: {', '.join(changed_paths)}"
    )
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "resizes/internal-technical-docs")
    owner, repo = repo_slug.split("/", 1) if "/" in repo_slug else ("resizes", "internal-technical-docs")
    git_commit_and_push(
        docs_dir,
        token=args.github_token,
        branch=args.docs_branch,
        message=commit_message,
        owner=owner,
        repo=repo,
    )
    print(f"Wiki updated: {', '.join(changed_paths)}")
    return 0


def main() -> int:
    args = parse_args()

    if args.apply_from_json:
        return apply_from_payload(args)

    source_dir = Path(args.source_dir).resolve()
    docs_dir = Path(args.docs_dir).resolve()
    config_path = source_dir / args.config

    config = load_config(config_path)
    docs_cfg = config.get("docs", {})
    behavior = config.get("behavior", {})
    max_files = int(behavior.get("max_files", 30))
    dry_run = args.dry_run or bool(behavior.get("dry_run", False))
    docs_branch = str(docs_cfg.get("branch", args.docs_branch))
    dispatch_owner = args.dispatch_owner or str(docs_cfg.get("owner", ""))
    dispatch_repo = args.dispatch_repo or str(docs_cfg.get("repository", ""))

    change_set = collect_commit_changes(source_dir, args.before_sha, args.after_sha, max_files)
    filtered = filter_changes(change_set.files, config)

    write_step_summary("## Wiki Sync\n")
    write_step_summary(f"- Source: `{args.source_repo}` @ `{args.after_sha[:7]}`")
    write_step_summary(f"- Docs repo: `{dispatch_owner}/{dispatch_repo}`")
    write_step_summary(f"- Changed files scanned: {len(change_set.files)}")
    write_step_summary(f"- Watched files matched: {len(filtered)}")

    if not filtered:
        write_step_summary("\n**Result:** No watched files changed. Skipping wiki sync.")
        print("No watched files changed.")
        return 0

    wiki_ctx = wiki_context(docs_dir)
    source_readme = read_text_file(source_dir / "README.md", 8000)
    prompt = build_prompt(
        source_repo=args.source_repo,
        change_set=change_set,
        filtered_files=filtered,
        wiki_ctx=wiki_ctx,
        source_readme=source_readme,
    )

    print("Calling LiteLLM for relevance analysis...")
    llm_response = call_litellm(
        base_url=args.litellm_base_url,
        api_key=args.litellm_api_key,
        model=args.litellm_model,
        prompt=prompt,
    )
    analysis = parse_analysis(llm_response)

    write_step_summary(f"\n**Relevant:** {analysis.relevant}")
    write_step_summary(f"**Reason:** {analysis.reason or 'n/a'}")

    if not analysis.relevant or not analysis.updates:
        print("Changes not relevant for wiki update.")
        return 0

    write_step_summary("\n### Proposed updates\n")
    for update in analysis.updates:
        write_step_summary(f"- `{update.path}` ({update.action})")

    if dry_run:
        write_step_summary("\n**Dry run:** no dispatch sent.")
        print("Dry run enabled; skipping wiki dispatch.")
        return 0

    if args.apply_local:
        if not args.github_token:
            raise RuntimeError("GITHUB_TOKEN is required for --apply-local")
        changed_paths = apply_updates(docs_dir, analysis.updates, log_entry=analysis.log_entry)
        commit_message = (
            f"docs(sync): update wiki from {args.source_repo}@{args.after_sha[:7]}\n\n"
            f"[skip ci]\n\n"
            f"Updated: {', '.join(changed_paths)}"
        )
    git_commit_and_push(
        docs_dir,
        token=args.github_token,
        branch=docs_branch,
        message=commit_message,
        owner=dispatch_owner,
        repo=dispatch_repo,
    )
        write_step_summary(f"\n**Committed paths:** {', '.join(changed_paths)}")
        print(f"Wiki updated locally: {', '.join(changed_paths)}")
        return 0

    if not args.github_token:
        raise RuntimeError("GITHUB_TOKEN is required to dispatch wiki updates")
    if not dispatch_owner or not dispatch_repo:
        raise RuntimeError("docs.owner and docs.repository must be set in wiki sync config")

    dispatch_wiki_sync(
        token=args.github_token,
        owner=dispatch_owner,
        repo=dispatch_repo,
        source_repo=args.source_repo,
        after_sha=args.after_sha,
        analysis=analysis,
    )

    write_step_summary(f"\n**Dispatched** wiki-sync event to `{dispatch_owner}/{dispatch_repo}`")
    print(f"Dispatched wiki-sync to {dispatch_owner}/{dispatch_repo}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - surface failure in CI
        write_step_summary(f"\n**Error:** {exc}")
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
