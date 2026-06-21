"""
GitHub Uploader Module
Handles authentication, branch management, and file commits to the factory-data branch.
"""

import os
import base64
import logging
import urllib3
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class GitHubUploader:
    """Handles all GitHub API interactions for the pipeline."""

    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)

        self.github_config = self.config["github"]
        self.pat = os.environ.get(self.github_config["pat_env_var"], "")
        self.owner = self.github_config["repo_owner"]
        self.repo = self.github_config["repo_name"]
        self.branch = self.github_config["data_branch"]
        self.base_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"
        self.headers = {
            "Authorization": f"token {self.pat}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "FactoryBot/1.0",
        }

        if not self.pat:
            logger.warning("GH_PAT not set. GitHub uploads will be skipped.")

    def _request(self, method: str, endpoint: str, **kwargs) -> Optional[dict]:
        """Make authenticated request to GitHub API."""
        url = f"{self.base_url}/{endpoint}"
        try:
            resp = requests.request(
                method, url, headers=self.headers, timeout=30, **kwargs
            )
            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.text else {}
            elif resp.status_code == 409:
                logger.warning(f"Branch conflict on {endpoint}: {resp.text}")
                return None
            elif resp.status_code == 403:
                logger.error(f"GitHub rate limit or auth failure: {resp.text}")
                return None
            else:
                logger.error(f"GitHub API {method} {endpoint}: {resp.status_code} - {resp.text}")
                return None
        except requests.RequestException as e:
            logger.error(f"GitHub API error: {e}")
            return None

    def ensure_branch(self) -> bool:
        """Create factory-data branch from main if it doesn't exist."""
        if not self.pat:
            return False

        # Check if branch exists
        result = self._request("GET", f"git/ref/heads/{self.branch}")
        if result:
            logger.info(f"Branch '{self.branch}' already exists.")
            return True

        # Get default branch (main or master)
        repo_info = self._request("GET", "")
        if not repo_info:
            return False

        default_branch = repo_info.get("default_branch", "main")

        # Get latest commit SHA from default branch
        ref = self._request("GET", f"git/ref/heads/{default_branch}")
        if not ref:
            return False

        sha = ref["object"]["sha"]

        # Create new branch
        result = self._request(
            "POST",
            "git/refs",
            json={"ref": f"refs/heads/{self.branch}", "sha": sha},
        )
        if result:
            logger.info(f"Created branch '{self.branch}' from {default_branch}.")
            return True
        return False

    def upload_file(self, local_path: str, repo_path: str, module_name: str = "pipeline") -> Optional[str]:
        """
        Upload a file to the factory-data branch.
        Returns the commit SHA or None on failure.
        """
        if not self.pat:
            logger.info(f"[DRY RUN] Would upload {local_path} -> {repo_path}")
            return None

        local_file = Path(local_path)
        if not local_file.exists():
            logger.error(f"File not found: {local_path}")
            return None

        content = local_file.read_bytes()
        content_b64 = base64.b64encode(content).decode("utf-8")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = self.github_config["commit_message_template"].format(
            module_name=module_name, timestamp=timestamp
        )

        # Check if file already exists to get SHA for update
        existing = self._request("GET", f"contents/{repo_path}?ref={self.branch}")
        existing_sha = existing.get("sha") if existing else None

        payload = {
            "message": message,
            "content": content_b64,
            "branch": self.branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        result = self._request("PUT", f"contents/{repo_path}", json=payload)
        if result and "commit" in result:
            commit_sha = result["commit"]["sha"]
            logger.info(f"Uploaded {repo_path} (commit: {commit_sha[:7]})")
            return commit_sha
        return None

    def upload_directory(self, local_dir: str, repo_prefix: str = "", module_name: str = "pipeline") -> list:
        """Upload all files in a directory to GitHub."""
        uploaded = []
        local_path = Path(local_dir)
        if not local_path.exists():
            return uploaded

        for file_path in local_path.rglob("*"):
            if file_path.is_file():
                relative = file_path.relative_to(local_path)
                repo_path = f"{repo_prefix}/{relative}".strip("/")
                sha = self.upload_file(str(file_path), repo_path, module_name)
                if sha:
                    uploaded.append({"file": str(relative), "sha": sha})
        return uploaded
