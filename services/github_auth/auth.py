#  Copyright (c) 2026 by Betware.
#  Holtasmári 1, Kópavogur, Iceland.
#  All rights reserved.
#
#  This software is the confidential and proprietary information
#  of Betware ("Confidential Information").  You
#  shall not disclose such Confidential Information and shall use
#  it only in accordance with the terms of the license agreement
#  you entered into with Betware.

# services/github_auth/auth.py
import time
import jwt
import requests
import os
from typing import Optional, Dict, Any

class GitHubAppAuth:


    """
    Handles GitHub App authentication using JWT and Installation Access Tokens (IAT).
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: Optional[str] = None,
        api_base: str = "https://api.github.com"
    ):
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.api_base = api_base.rstrip("/")
        self._token: Optional[str] = None
        self._token_expires_at: float = 0

    def _generate_jwt(self) -> str:
        """
        Generates a JWT for authenticating as the GitHub App.
        """
        now = int(time.time())
        payload = {
            "iat": now - 60,  # 60 seconds in the past for clock skew
            "exp": now + (10 * 60),  # 10 minutes maximum
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def get_installation_id(self, owner: str, repo: str) -> str:
        """
        Retrieves the installation ID for a specific repository if not provided.
        """
        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"{self.api_base}/repos/{owner}/{repo}/installation"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return str(resp.json()["id"])

    def get_token(self, force_refresh: bool = False) -> str:
        """
        Returns a valid Installation Access Token (IAT).
        Refreshes if expired or force_refresh is True.
        """
        now = time.time()
        if not force_refresh and self._token and now < self._token_expires_at - 60:
            return self._token

        if not self.installation_id:
            raise ValueError("installation_id is required to get an access token")

        jwt_token = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"{self.api_base}/app/installations/{self.installation_id}/access_tokens"
        resp = requests.post(url, headers=headers, timeout=10)
        resp.raise_for_status()

        data = resp.json()
        self._token = data["token"]
        # GitHub tokens typically last 1 hour
        # expires_at format: "2024-03-02T11:27:00Z"
        expires_at_str = data["expires_at"].replace("Z", "+00:00")
        from datetime import datetime
        expires_at_dt = datetime.fromisoformat(expires_at_str)
        self._token_expires_at = expires_at_dt.timestamp()

        return self._token

_GH_APP_AUTH_INSTANCE: Optional[GitHubAppAuth] = None

def get_github_app_auth(config: Dict[str, Any]) -> Optional[GitHubAppAuth]:
    """
    Factory function to create or return a cached GitHubAppAuth instance.
    """
    global _GH_APP_AUTH_INSTANCE

    # Return cached instance if it already exists
    if _GH_APP_AUTH_INSTANCE:
        return _GH_APP_AUTH_INSTANCE

    app_id = config.get("GITHUB_APP_ID") or os.getenv("GITHUB_APP_ID")
    private_key = config.get("GITHUB_PRIVATE_KEY") or os.getenv("GITHUB_PRIVATE_KEY")
    installation_id = config.get("GITHUB_INSTALLATION_ID") or os.getenv("GITHUB_INSTALLATION_ID")
    api_base = config.get("GITHUB_API") or os.getenv("GITHUB_API") or "https://api.github.com"

    if app_id and private_key:
        # (Keep the existing private_key file reading logic here...)
        if private_key.startswith("/") or (len(private_key) < 255 and os.path.exists(private_key)):
            try:
                with open(private_key, "r") as f:
                    private_key = f.read()
            except Exception:
                pass

        # Cache and return the new instance
        _GH_APP_AUTH_INSTANCE = GitHubAppAuth(
            app_id=app_id,
            private_key=private_key,
            installation_id=installation_id,
            api_base=api_base
        )
        return _GH_APP_AUTH_INSTANCE

    return None
