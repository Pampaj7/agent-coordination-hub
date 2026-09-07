"""GitHub integration.

Scope is deliberately tiny: the relay *references* GitHub, it does not reimplement
it. Two jobs only:

1. Turn relay identifiers into GitHub URLs (``GH-142`` -> issue 142, a branch name
   -> its tree URL, a 7-40 char hex string -> a commit URL). Pure string work, no
   network, always available once owner/repo are set.
2. Optionally *read* issue and PR metadata. Never writes. Any failure degrades to
   ``None`` -- GitHub being down must never stop an agent from logging an event.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from agent_relay.config import Settings, get_settings

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class GitHubService:
    """Read-only GitHub helper. Safe to construct even when nothing is configured."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ links

    @property
    def enabled(self) -> bool:
        return self.settings.github_enabled

    @property
    def repo_url(self) -> str | None:
        if not self.enabled:
            return None
        return f"https://github.com/{self.settings.github_owner}/{self.settings.github_repo}"

    def issue_number(self, task: str | None) -> int | None:
        """``GH-142`` -> ``142``. Anything else -> ``None``."""
        if not task:
            return None
        prefix = self.settings.github_task_prefix
        if prefix and task.upper().startswith(prefix.upper()):
            rest = task[len(prefix) :]
            if rest.isdigit():
                return int(rest)
        return None

    def task_url(self, task: str | None) -> str | None:
        number = self.issue_number(task)
        if number is None or not self.repo_url:
            return None
        # GitHub redirects /issues/<n> to the PR when <n> is a PR, so one form covers both.
        return f"{self.repo_url}/issues/{number}"

    def branch_url(self, branch: str | None) -> str | None:
        if not branch or not self.repo_url:
            return None
        return f"{self.repo_url}/tree/{branch}"

    def commit_url(self, sha: str) -> str | None:
        if not self.repo_url or not _COMMIT_SHA.match(sha.strip().lower()):
            return None
        return f"{self.repo_url}/commit/{sha.strip()}"

    def pr_url(self, number: int) -> str | None:
        return f"{self.repo_url}/pull/{number}" if self.repo_url else None

    def artifact_url(self, artifact: str) -> str | None:
        """Best-effort link for an artifact string: a commit sha, or a repo path."""
        item = artifact.strip()
        if not item or not self.repo_url:
            return None
        if item.startswith(("http://", "https://")):
            return item
        if (sha := self.commit_url(item)) is not None:
            return sha
        if item.startswith("commit ") and (sha := self.commit_url(item[7:])) is not None:
            return sha
        return None

    def links_for(self, *, task: str | None = None, branch: str | None = None) -> dict[str, str]:
        """Every link we can build for a task/branch pair, omitting the ones we cannot."""
        links = {"task": self.task_url(task), "branch": self.branch_url(branch)}
        return {k: v for k, v in links.items() if v}

    # ------------------------------------------------------------------ reads

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return headers

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        if not self.enabled:
            return None
        url = f"{API_ROOT}/repos/{self.settings.github_owner}/{self.settings.github_repo}{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self._headers(), params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:  # 404 for a task that is not an issue is normal
            logger.info("github GET %s -> %s", path, exc.response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("github GET %s failed: %s", path, type(exc).__name__)
        return None

    async def get_issue(self, task_or_number: str | int) -> dict[str, Any] | None:
        """Title/state/labels for an issue, or None if unavailable."""
        number = (
            task_or_number if isinstance(task_or_number, int) else self.issue_number(task_or_number)
        )
        if number is None:
            return None
        data = await self._get(f"/issues/{number}")
        if not isinstance(data, dict):
            return None
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "url": data.get("html_url"),
            "labels": [label.get("name") for label in data.get("labels", []) if label.get("name")],
            "assignees": [a.get("login") for a in data.get("assignees", []) if a.get("login")],
            "is_pull_request": "pull_request" in data,
        }

    async def list_open_pulls(self, limit: int = 20) -> list[dict[str, Any]]:
        """Open PR metadata (number, title, branch, author). Empty list on any failure."""
        data = await self._get("/pulls", params={"state": "open", "per_page": limit})
        if not isinstance(data, list):
            return []
        return [
            {
                "number": pr.get("number"),
                "title": pr.get("title"),
                "branch": (pr.get("head") or {}).get("ref"),
                "base": (pr.get("base") or {}).get("ref"),
                "author": (pr.get("user") or {}).get("login"),
                "draft": pr.get("draft", False),
                "url": pr.get("html_url"),
            }
            for pr in data
        ]
