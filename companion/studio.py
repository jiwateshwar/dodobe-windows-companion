"""Restarting and shutting the box down through the studio, as an Admin would on Settings > System.

The companion signs in with the person's Dodobe name and password (POST /login), sends the same
form the System page does (POST /settings/system/power, action=restart|shutdown, sure=1), and signs
out. It talks to the box's gateway on the local network (http://<box>), not through the internet.
The studio refuses state-changing requests that don't come from its own site, so each one carries
the site's own Origin; that is a browser protection, and the companion is the signed-in user's own
client. Only Admins may do it: the studio refuses anyone else.
"""

import http.client
from urllib.parse import parse_qs, urlencode, urlsplit

TIMEOUT = 20
AGENT = "dodobe-companion"


class StudioError(Exception):
    pass


class Studio:
    def __init__(self, base: str, timeout: float = TIMEOUT):
        u = urlsplit(base)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise StudioError(f"{base} isn't a web address")
        self.scheme, self.netloc, self.host, self.port = u.scheme, u.netloc, u.hostname, u.port
        self.timeout = timeout
        self.cookie: str | None = None

    def _post(self, path: str, form: dict) -> tuple[int, dict, str]:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        headers = {"Origin": f"{self.scheme}://{self.netloc}", "User-Agent": AGENT,
                   "Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            conn.request("POST", path, body=urlencode(form), headers=headers)
            r = conn.getresponse()
            body = r.read(65536).decode("utf-8", "replace")
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body
        except OSError as exc:
            raise StudioError(f"Dodobe's studio can't be reached at {self.scheme}://{self.netloc} ({exc}).") from exc
        finally:
            conn.close()

    def login(self, username: str, password: str) -> None:
        status, headers, body = self._post("/login", {"username": username, "password": password,
                                                      "next": "/settings/system"})
        if status == 401:
            raise StudioError("Dodobe didn't accept your name and password.")
        if status == 429:
            raise StudioError("Too many sign-in attempts: Dodobe asks to wait 15 minutes.")
        if status == 403:
            raise StudioError("Dodobe's studio refused the request from this app.")
        cookie = headers.get("set-cookie", "").split(";", 1)[0]
        if status not in (302, 303) or "=" not in cookie:
            raise StudioError(f"Signing in to Dodobe didn't work (HTTP {status}).")
        self.cookie = cookie

    def power(self, action: str) -> None:
        """action: "restart" or "shutdown". Raises StudioError with the studio's reason."""
        if action not in ("restart", "shutdown"):
            raise ValueError(action)
        status, headers, body = self._post("/settings/system/power", {"action": action, "sure": "1"})
        if status == 200:
            return
        where = headers.get("location", "")
        if status == 403:
            raise StudioError("Only a Dodobe Admin can restart or shut down the box."
                              if "Cross-site request refused" not in body else "Dodobe's studio refused the request from this app.")
        if status in (302, 303):
            path = urlsplit(where).path
            if path.startswith("/account/password"):
                raise StudioError("Dodobe asks you to change your password first: sign in on the homepage.")
            if path.startswith("/login"):
                raise StudioError("Dodobe signed the companion out before it could ask.")
            error = parse_qs(urlsplit(where).query).get("error")
            if error:
                raise StudioError(f"Dodobe said: {error[0]}")
        raise StudioError(f"Dodobe didn't do it (HTTP {status}).")

    def logout(self) -> None:
        if self.cookie:
            try:
                self._post("/logout", {})
            except StudioError:
                pass  # the box may already be going down
            self.cookie = None


def power(base: str, username: str, password: str, action: str) -> None:
    s = Studio(base)
    s.login(username, password)
    try:
        s.power(action)
    finally:
        s.logout()
