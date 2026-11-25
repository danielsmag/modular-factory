"""Jira Cloud's REST API over HTTPS, with the standard library only.

The account and its API token are read from the environment when the first request is made. They go
only to the configured site, in the Authorization header, and never into an error message or a file.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...util import Refused

PAGE = 100
# How long one 429 answer may make a command wait before it gives up.
MAX_WAIT_SECONDS = 20.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The credentials are for one site. A redirect is an error, not somewhere to send them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, site: str, email_env: str, token_env: str, timeout: float = 30.0):
        self.site = site.rstrip("/")
        self.email_env = email_env
        self.token_env = token_env
        self.timeout = timeout
        handlers: list[Any] = [_NoRedirect()]
        if urllib.parse.urlsplit(self.site).hostname in ("127.0.0.1", "localhost"):
            handlers.append(urllib.request.ProxyHandler({}))  # a server on this machine is never behind a proxy
        self._opener = urllib.request.build_opener(*handlers)

    def _authorization(self) -> str:
        values: list[str] = []
        for name, what in ((self.email_env, "email_env"), (self.token_env, "token_env")):
            value: str = os.environ.get(name, "")
            if not value:
                raise Refused(f"Jira needs {name} in the environment ([tracker.jira].{what}). "
                              "Export it in the shell that runs the factory.")
            values.append(value)
        return "Basic " + base64.b64encode(":".join(values).encode()).decode()

    def _problem(self, error: urllib.error.HTTPError, what: str) -> Refused:
        if error.code == 401:
            return Refused(f"Jira refused the account in {self.email_env} and the token in {self.token_env}")
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
