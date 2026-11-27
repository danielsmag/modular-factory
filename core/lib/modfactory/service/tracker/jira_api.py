"""Jira Cloud's REST API over HTTPS, with the standard library only.

The account email and the API token are read when the first request is made. They go only to the
configured site, in the Authorization header, and never into an error message or a file. The token
value is never a config key: a path, the process environment, or one key in `.env`.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ...util import Refused

PAGE = 100
# How long one 429 answer may make a command wait before it gives up.
MAX_WAIT_SECONDS = 20.0
_DOTENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def account_email(settings: dict[str, Any]) -> tuple[str, str]:
    """The account address and where it came from.

    A non-empty `[tracker.jira].email` wins, even when `email_env` is also set. Otherwise the
    process environment. `.env` is not a source for the email.
    """
    literal: str = str(settings.get("email") or "").strip()
    env_name: str = str(settings.get("email_env") or "JIRA_EMAIL")
    if literal:
        return literal, "factory.toml"
    return os.environ.get(env_name, ""), f"${env_name}"


def account_token(settings: dict[str, Any], repo: Path) -> tuple[str, str]:
    """The API token and where it came from.

    A non-empty `token_file` is the only source. Otherwise `token_env` in the process environment,
    then the same key in `<repo>/.env`. A set `token_file` that cannot be used raises `Refused`.
    The token is `""` when no source has one.
    """
    raw_path: str = str(settings.get("token_file") or "").strip()
    env_name: str = str(settings.get("token_env") or "JIRA_API_TOKEN")
    if raw_path:
        return _read_token_file(raw_path, repo), "token_file"
    from_env: str = os.environ.get(env_name, "")
    if from_env:
        return from_env, f"${env_name}"
    from_file: str = _dotenv_value(repo / ".env", env_name)
    if from_file:
        return from_file, ".env"
    return "", f"${env_name}"


def rest_ready(settings: dict[str, Any], repo: Path) -> bool:
    """True when an email and a token are both found. A bad `token_file` propagates `Refused`."""
    email: str = account_email(settings)[0]
    token: str = account_token(settings, repo)[0]
    return bool(email) and bool(token)


def _token_path(raw: str, repo: Path) -> Path:
    """`raw` under `repo`, or an absolute path. `~` is the operator's home."""
    expanded: Path = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded
    return repo / expanded


def _read_token_file(raw: str, repo: Path) -> str:
    """The one line in the token file. The line itself never goes into the error."""
    path: Path = _token_path(raw, repo)
    try:
        text: str = path.read_text()
    except OSError as error:
        reason: str = error.strerror or str(error)
        raise Refused(f"Jira could not read [tracker.jira].token_file ({path}): {reason}") from None
    lines: list[str] = text.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise Refused(f"[tracker.jira].token_file must contain only the token ({path})")
    return lines[0].strip()


def _dotenv_value(path: Path, key: str) -> str:
    """One key from a `.env` file. The file is not loaded into the environment.

    Blank lines and `#` comments are ignored. `export KEY=value` and matching quotes are accepted.
    `$` is not expanded. The first matching key wins. A missing file is an empty value.
    """
    try:
        text: str = path.read_text()
    except OSError:
        return ""
    line: str
    for line in text.splitlines():
        stripped: str = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        found: re.Match[str] | None = _DOTENV_LINE.fullmatch(stripped)
        if found is None or found.group(1) != key:
            continue
        value: str = found.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The credentials are for one site. A redirect is an error, not somewhere to send them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, site: str, email_env: str, token_env: str, repo: Path, email: str = "",
                 token_file: str = "", timeout: float = 30.0) -> None:
        self.site = site.rstrip("/")
        self.email_env = email_env
        self.token_env = token_env
        self.repo = repo
        self.email = email
        self.token_file = token_file
        self.timeout = timeout
        self._email_from: str = f"${email_env}"
        self._token_from: str = f"${token_env}"
        handlers: list[Any] = [_NoRedirect()]
        if urllib.parse.urlsplit(self.site).hostname in ("127.0.0.1", "localhost"):
            handlers.append(urllib.request.ProxyHandler({}))  # a server on this machine is never behind a proxy
        self._opener = urllib.request.build_opener(*handlers)

    def _settings(self) -> dict[str, Any]:
        return {"email": self.email, "email_env": self.email_env, "token_env": self.token_env,
                "token_file": self.token_file}

    def credential_sources(self) -> tuple[str, str]:
        """Where the email and the token come from. Neither value is returned."""
        settings: dict[str, Any] = self._settings()
        email_from: str = account_email(settings)[1]
        token_from: str = account_token(settings, self.repo)[1]
        return email_from, token_from

    def _authorization(self) -> str:
        settings: dict[str, Any] = self._settings()
        email: str
        email_from: str
        email, email_from = account_email(settings)
        token: str
        token_from: str
        token, token_from = account_token(settings, self.repo)
        self._email_from = email_from
        self._token_from = token_from
        if not email:
            raise Refused(f"Jira needs {self.email_env} in the environment ([tracker.jira].email_env). Export it in "
                          "the shell that runs the factory, or set [tracker.jira].email, or install the Atlassian "
                          "CLI and run `acli jira auth login`: the factory falls back to acli.")
        if not token:
            raise Refused(f"Jira needs {self.token_env} in the environment ([tracker.jira].token_env), in "
                          f"{self.repo / '.env'}, or in [tracker.jira].token_file. Export it in the shell that "
                          "runs the factory, or install the Atlassian CLI and run `acli jira auth login`: the "
                          "factory falls back to acli.")
        return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def _problem(self, error: urllib.error.HTTPError, what: str) -> Refused:
        if error.code == 401:
            return Refused(f"Jira refused the account in {self._email_from} and the token in {self._token_from}")
        try:
            found: object = json.loads(error.read() or b"{}")
        except (ValueError, OSError):
            found = {}
        said: list[str] = []
        if isinstance(found, dict):
            messages: object = found.get("errorMessages")
            said += [str(m) for m in messages] if isinstance(messages, list) else []
            fields: object = found.get("errors")
            said += [f"{k}: {v}" for k, v in fields.items()] if isinstance(fields, dict) else []
        return Refused(f"Jira answered {error.code} to {what}" + (f": {'; '.join(said)}" if said else ""))

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, body: object = None) -> Any:
        query: str = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        url: str = f"{self.site}{urllib.parse.quote(path)}" + (f"?{query}" if query else "")
        headers: dict[str, str] = {"Authorization": self._authorization(), "Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        what: str = f"{method} {path}"
        for attempt in (1, 2):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(req, timeout=self.timeout) as res:
                    raw: bytes = res.read()
                    return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt == 1:
                    try:
                        wait: float = float(error.headers.get("Retry-After") or 2)
                    except ValueError:
                        wait = 2.0
                    time.sleep(min(max(wait, 0.0), MAX_WAIT_SECONDS))
                    continue
                if 300 <= error.code < 400:
                    raise Refused(f"Jira redirected {what}; check [tracker.jira].site") from None
                raise self._problem(error, what) from None
            except (urllib.error.URLError, OSError) as error:
                reason: object = getattr(error, "reason", error)
                raise Refused(f"could not reach Jira at {self.site}: {reason}") from None
            except ValueError:
                raise Refused(f"Jira did not answer {what} with JSON; check [tracker.jira].site") from None
        raise Refused(f"Jira is limiting requests ({what}); try again in a moment")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params)

    def post(self, path: str, body: object, **params: Any) -> Any:
        return self.request("POST", path, params, body)

    def pages(self, method: str, path: str, key: str, body: object = None, limit: int | None = None,
              **params: Any) -> list[Any]:
        """Every item of a paged list, read `PAGE` at a time. `key` is the field that holds one page.

        `limit` stops reading once that many items are in hand.
        """
        out: list[Any] = []
        while True:
            if limit is not None and len(out) >= limit:
                return out
            found: object = self.request(method, path, dict(params, startAt=len(out), maxResults=PAGE), body)
            page: object = found.get(key) if isinstance(found, dict) else None
            if not isinstance(page, list) or not page:
                return out
            out += page
            total: object = found.get("total") if isinstance(found, dict) else None
            if (isinstance(found, dict) and found.get("isLast") is True) or (
                    isinstance(total, int) and len(out) >= total):
                return out
