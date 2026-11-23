from fastapi.testclient import TestClient

from agent import instance_auth


class PairedBrowserClient(TestClient):
    """Use an ephemeral session for tests that exercise owner browser APIs."""

    def __init__(self, app, base_url="http://127.0.0.1", *, headers=None, **kwargs):
        super().__init__(app, base_url=base_url,
                         headers={"Origin": base_url, **(headers or {})}, **kwargs)
        credentials = instance_auth.instance_credentials()
        self.cookies.set(instance_auth.SESSION_COOKIE, credentials.redeem(credentials.issue()))

    def websocket_connect(self, url, subprotocols=None, **kwargs):
        websocket_origin = str(self.base_url).replace("http", "ws", 1).rstrip("/")
        return super().websocket_connect(websocket_origin + url, subprotocols, **kwargs)
