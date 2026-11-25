"""Talking to the Sprite we live in (checkpoints, keep-awake) and to sibling Sprites (Parallel Lives)."""
import contextlib
import json
import logging
import re
import subprocess
import uuid

import httpx

from . import config

log = logging.getLogger("cc.sprite")
_sock = httpx.Client(transport=httpx.HTTPTransport(uds="/.sprite/api.sock"), base_url="http://sprite", timeout=30)


# ---------------------------------------------------------------- self: checkpoints
def checkpoint_create(comment: str) -> str | None:
    """Snapshot the whole filesystem (~1s). Returns the version id, e.g. 'v7'."""
    try:
        out = subprocess.run(["sprite-env", "checkpoints", "create", "--comment", comment[:120]],
                             capture_output=True, text=True, timeout=90).stdout
        m = re.findall(r"Checkpoint (v\d+) created", out)
        return m[-1] if m else None
    except Exception as e:
        log.error("checkpoint failed: %s", e)
        return None


def checkpoint_list() -> list[dict]:
    try:
        out = subprocess.run(["sprite-env", "checkpoints", "list"], capture_output=True, text=True, timeout=30).stdout
        return [c for c in json.loads(out) if c.get("id") != "Current"]
    except Exception as e:
        log.error("checkpoint list failed: %s", e)
        return []


def checkpoint_restore(version: str) -> None:
    """Async: the environment restarts right after this returns. Do everything else first."""
    subprocess.Popen(["sprite-env", "checkpoints", "restore", version],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ---------------------------------------------------------------- self: keep awake while working
@contextlib.contextmanager
def keep_awake(minutes: int = 15):
    name = f"work-{uuid.uuid4().hex[:8]}"
    try:
        _sock.post("/v1/tasks", json={"name": name, "expire": f"{minutes}m"})
    except Exception as e:
        log.warning("keep-awake task failed: %s", e)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            _sock.delete(f"/v1/tasks/{name}")


# ---------------------------------------------------------------- siblings: Sprites REST API
class Sprites:
    def __init__(self, token: str = config.SPRITES_TOKEN):
        self.c = httpx.Client(base_url=config.SPRITES_API, headers={"Authorization": f"Bearer {token}"},
                              timeout=httpx.Timeout(600, connect=20))

    @property
    def ready(self) -> bool:
        return bool(config.SPRITES_TOKEN)

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise RuntimeError(f"sprites api {r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:300]}")
        return r

    def create(self, name: str) -> dict:
        r = self.c.post("/v1/sprites", json={"name": name})
        if r.status_code == 409:
            return self.get(name)
        return self._check(r).json()

    def get(self, name: str) -> dict:
        return self._check(self.c.get(f"/v1/sprites/{name}")).json()

    def list_all(self) -> list[dict]:
        data = self._check(self.c.get("/v1/sprites")).json()
        return data.get("sprites", data) if isinstance(data, dict) else data

    def destroy(self, name: str) -> None:
        r = self.c.delete(f"/v1/sprites/{name}")
        if r.status_code not in (200, 204, 404):
            self._check(r)

    def write_file(self, name: str, path: str, data: bytes, mode: str = "0644") -> None:
        self._check(self.c.put(f"/v1/sprites/{name}/fs/write",
                               params={"path": path, "workingDir": "/home/sprite", "mkdir": "true", "mode": mode},
                               content=data, headers={"Content-Type": "application/octet-stream"}))

    def read_file(self, name: str, path: str) -> bytes:
        return self._check(self.c.get(f"/v1/sprites/{name}/fs/read",
                                      params={"path": path, "workingDir": "/home/sprite"})).content

    def exec(self, name: str, cmd: list[str], timeout: float = 600) -> str:
        params = [("cmd", c) for c in cmd]
        r = self.c.post(f"/v1/sprites/{name}/exec", params=params, timeout=timeout)
        return self._check(r).text


sprites = Sprites()
