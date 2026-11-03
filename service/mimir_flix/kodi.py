"""Play on the TV through Kodi's JSON-RPC: a link, or a provider's own Kodi add-on (a plugin:// address)."""

import httpx


class KodiError(Exception):
    pass


class Kodi:
    def __init__(self, url: str, password: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=url, auth=("kodi", password), timeout=15, transport=transport)

    def rpc(self, method: str, params: dict | None = None):
        try:
            r = self.http.post("/jsonrpc", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        except httpx.HTTPError as e:
            raise KodiError("Kodi is not answering: is the TV on and Kodi open?") from e
        if r.status_code == 401:
            raise KodiError("Kodi refused the password")
        data = r.json()
        if "error" in data:
            raise KodiError(f"Kodi: {data['error'].get('message')}")
        return data.get("result")

    def play(self, file: str) -> None:
        self.rpc("Player.Open", {"item": {"file": file}})
