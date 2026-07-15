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
    return any(fnmatch.fnmatch(nor