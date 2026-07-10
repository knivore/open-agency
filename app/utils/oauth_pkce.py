import asyncio
import base64
import certifi
import hashlib
import httpx
import json
import os
import secrets
import threading
import time
import urllib.parse
import uvicorn
import webbrowser
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from typing import Any, Dict, Optional

OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
OPENAI_CODEX_SCOPE = "openid profile email offline_access"
LEGACY_OPENAI_CODEX_CLIENT_IDS = {"app_EMoaD9zS2S", "DEFAULT_CLIENT_ID", ""}
LEGACY_OPENAI_CODEX_REDIRECT_URIS = {"http://127.0.0.1:1455/auth/callback"}


def _missing_ca_bundle_env_var() -> Optional[str]:
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(name)
        if value and not os.path.exists(value):
            return name
    return None


def _oauth_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an OAuth HTTP client resilient to stale local CA env vars."""
    if "verify" not in kwargs and _missing_ca_bundle_env_var():
        kwargs["verify"] = certifi.where()
    return httpx.AsyncClient(**kwargs)


class OAuthPKCEHandler:
    def __init__(
            self,
            auth_url: str = "https://auth.openai.com/oauth/authorize",
            token_url: str = "https://auth.openai.com/oauth/token",
            redirect_uri: str = OPENAI_CODEX_REDIRECT_URI,
            client_id: str = "DEFAULT_CLIENT_ID",  # This might need to be configurable
            scope: str = OPENAI_CODEX_SCOPE,
            state: Optional[str] = None
    ):
        self.auth_url = auth_url
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.client_id = client_id
        self.scope = scope
        self.state = state or secrets.token_urlsafe(16)
        self.extra_params = {}  # Optional extra parameters for authorization
        self.code_verifier = ""
        self.code_challenge = ""
        self.authorization_code = ""
        self._server_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @classmethod
    def for_provider(cls, provider_type: str, client_id: str, redirect_uri: Optional[str] = None,
                     tenant_id: Optional[str] = None) -> "OAuthPKCEHandler":
        """Factory method to create a handler for a specific provider."""
        # Normalize client_id: Treat "DEFAULT_CLIENT_ID" or empty as None
        cid_normalized = client_id if client_id and client_id != "DEFAULT_CLIENT_ID" else None

        # Standard default redirect
        default_redirect = "http://127.0.0.1:1455/auth/callback"

        if provider_type == "openai_codex":
            # Match the ChatGPT OAuth client used by OpenClaw's current pi-ai flow.
            if client_id in LEGACY_OPENAI_CODEX_CLIENT_IDS:
                cid_normalized = None
            cid = cid_normalized or OPENAI_CODEX_CLIENT_ID
            if redirect_uri in LEGACY_OPENAI_CODEX_REDIRECT_URIS:
                redirect_uri = None

            # Use the provided redirect_uri. We no longer force a fallback to openclaw.org
            # because the user wants to control their own redirection destination.
            # If the user provides "localhost", we respect it.
            return cls(
                auth_url="https://auth.openai.com/oauth/authorize",
                token_url="https://auth.openai.com/oauth/token",
                redirect_uri=redirect_uri or OPENAI_CODEX_REDIRECT_URI,
                client_id=cid,
                scope=OPENAI_CODEX_SCOPE
            )
        elif provider_type == "google":
            return cls(
                auth_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",
                redirect_uri=redirect_uri or default_redirect,
                client_id=client_id,
                scope="https://www.googleapis.com/auth/cloud-platform openid email profile"
            )
        elif provider_type == "azure_openai":
            tenant = tenant_id or "common"
            return cls(
                auth_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
                token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                redirect_uri=redirect_uri or default_redirect,
                client_id=client_id,
                scope="https://cognitiveservices.azure.com/.default openid offline_access"
            )
        else:
            # Default or generic
            return cls(client_id=client_id, redirect_uri=redirect_uri or default_redirect)

    def generate_pkce_data(self):
        """Step 1: Initialize the PKCE Challenge"""
        # Generate a high-entropy random string
        self.code_verifier = secrets.token_urlsafe(64)

        # Hash it to create the challenge
        sha256_hash = hashlib.sha256(self.code_verifier.encode('ascii')).digest()
        self.code_challenge = base64.urlsafe_b64encode(sha256_hash).decode('ascii').replace('=', '')
        return self.code_verifier, self.code_challenge

    def get_authorization_url(self) -> str:
        if not self.code_challenge:
            self.generate_pkce_data()

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scope,
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
        }
        if self.auth_url.startswith("https://auth.openai.com"):
            params["id_token_add_organizations"] = "true"
            params["codex_cli_simplified_flow"] = "true"
            params["originator"] = "agency"

        params.update(self.extra_params)
        query_string = urllib.parse.urlencode(params)
        return f"{self.auth_url}?{query_string}"

    @staticmethod
    def parse_redirect_url(redirect_url: str) -> Dict[str, Optional[str]]:
        parsed = urllib.parse.urlparse(redirect_url)
        query = urllib.parse.parse_qs(parsed.query)
        return {
            "code": query.get("code", [None])[0],
            "state": query.get("state", [None])[0],
            "error": query.get("error", [None])[0],
            "error_description": query.get("error_description", [None])[0],
        }

    @staticmethod
    def extract_account_id(access_token: Optional[str]) -> Optional[str]:
        if not access_token or access_token.count(".") < 2:
            return None
        try:
            payload = access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
            claims = json.loads(decoded)
        except Exception:
            return None

        for key in (
                "accountId",
                "account_id",
                "https://api.openai.com/account_id",
                "https://api.openai.com/accountId",
        ):
            value = claims.get(key)
            if isinstance(value, str) and value:
                return value
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            value = auth_claims.get("chatgpt_account_id")
            if isinstance(value, str) and value:
                return value
        return None

    async def exchange_token(self, code: str, code_verifier: str) -> Dict:
        """Step 3: The Token Exchange"""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": self.redirect_uri,
        }

        async with _oauth_async_client() as client:
            response = await client.post(self.token_url, data=payload)
            response.raise_for_status()
            return response.json()

    async def refresh_token(self, refresh_token: str) -> Dict:
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
        }

        async with _oauth_async_client() as client:
            response = await client.post(self.token_url, data=payload)
            response.raise_for_status()
            return response.json()

    async def initiate_device_auth(self) -> Dict:
        """Step 1 for Device Code Flow: Request a device code"""
        payload = {
            "client_id": self.client_id,
            "scope": self.scope
        }
        parsed = urllib.parse.urlparse(self.auth_url)
        authorize_path = parsed.path or "/oauth/authorize"
        if authorize_path.endswith("/authorize"):
            device_code_path = authorize_path[: -len("/authorize")] + "/device/code"
        else:
            device_code_path = "/oauth/device/code"
        device_code_url = urllib.parse.urlunparse(parsed._replace(path=device_code_path, query=""))
        async with _oauth_async_client() as client:
            response = await client.post(device_code_url, data=payload)
            response.raise_for_status()
            return response.json()

    async def poll_device_token(self, device_code: str, interval: int = 5) -> Dict:
        """Step 2 for Device Code Flow: Poll for a token"""
        payload = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": self.client_id,
            "device_code": device_code,
        }
        async with _oauth_async_client() as client:
            while True:
                response = await client.post(self.token_url, data=payload)
                data = response.json()
                if response.status_code == 200:
                    return data
                elif data.get("error") == "authorization_pending":
                    await asyncio.sleep(interval)
                else:
                    response.raise_for_status()

    def start_callback_server(self, port: int = 1455):
        """Step 2: Create a Local Callback Server"""
        import socket
        app = FastAPI()
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        callback_path = parsed_redirect.path or "/auth/callback"
        bind_host = parsed_redirect.hostname or "127.0.0.1"
        if bind_host == "localhost":
            bind_host = "127.0.0.1"

        async def callback(request: Request):
            code = request.query_params.get("code")
            state = request.query_params.get("state")

            if state and state != self.state:
                self._stop_event.set()
                server.should_exit = True
                return HTMLResponse(content="<h1>Authorization failed!</h1><p>Invalid state parameter.</p>",
                                    status_code=400)

            if code:
                self.authorization_code = code
                self._stop_event.set()
                server.should_exit = True
                return HTMLResponse(content="""
                    <html>
                        <head>
                            <script>
                                (function () {
                                    var message = {
                                        type: "agency-oauth-callback",
                                        redirectUrl: window.location.href
                                    };
                                    if (window.opener && !window.opener.closed) {
                                        window.opener.postMessage(message, "*");
                                        window.setTimeout(function () {
                                            window.close();
                                        }, 750);
                                    }
                                }());
                            </script>
                        </head>
                        <body style="font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; background: #f9fafb;">
                            <div style="background: white; padding: 2rem; border-radius: 0.5rem; box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.1); text-align: center;">
                                <h1 style="color: #059669;">Authorization Successful!</h1>
                                <p>Returning to the Agency app...</p>
                            </div>
                        </body>
                    </html>
                """)
            self._stop_event.set()
            server.should_exit = True
            return HTMLResponse(content="<h1>Authorization failed!</h1><p>No code received.</p>", status_code=400)

        app.add_api_route(callback_path, callback, methods=["GET"])

        config = uvicorn.Config(app, host=bind_host, port=port, log_level="error")
        server = uvicorn.Server(config)

        def run_server():
            # Check if port is available
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.settimeout(1)
                    s.bind((bind_host, port))
                except socket.error:
                    # Port already in use, don't start server
                    import logging
                    logging.error(f"OAuth callback server failed to start: {bind_host}:{port} already in use.")
                    return
            server.run()

        self._server_thread = threading.Thread(target=run_server, daemon=True)
        self._server_thread.start()

    def wait_for_code(self, timeout: int = 300) -> Optional[str]:
        start_time = time.time()
        while not self._stop_event.is_set():
            if time.time() - start_time > timeout:
                return None
            time.sleep(1)
        return self.authorization_code


def run_oauth_flow(client_id: str = "DEFAULT_CLIENT_ID") -> Optional[Dict]:
    handler = OAuthPKCEHandler(client_id=client_id)
    handler.generate_pkce_data()
    auth_url = handler.get_authorization_url()

    print(f"Opening browser for authorization: {auth_url}")
    webbrowser.open(auth_url)

    handler.start_callback_server()
    code = handler.wait_for_code()

    if code:
        import asyncio
        return asyncio.run(handler.exchange_token(code, handler.code_verifier))
    return None
