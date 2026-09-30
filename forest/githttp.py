"""Git smart-HTTP for vault sync:  git clone https://host/git/<vault>.git

Bridges to `git http-backend` (CGI). Auth happens in the API middleware:
fetch/clone needs a read token, push needs a write token (use the token as the password).
Pushes must be fast-forward and update the server's working tree (receive.denyCurrentBranch=updateInstead).
"""

from __future__ import annotations

import os
import subprocess

from fastapi import HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from . import auth
from .store import vault

SERVICES = {"git-upload-pack", "git-receive-pack"}


def required_scope(rest: str, service_q: str | None) -> str:
    svc = service_q if rest == "info/refs" else rest
    return "write" if svc == "git-receive-pack" else "read"


async def handle(request: Request, vault_name: str, rest: str) -> Response:
    vault_name = vault_name.removesuffix(".git")
    try:
        v = vault(vault_name)
    except KeyError:
        raise HTTPException(404, "unknown vault")
    p = auth.principal.get()
    if not p.can_access(v.name):
        raise HTTPException(403, "token not allowed for this vault")

    service_q = request.query_params.get("service")
    if rest == "info/refs":
        if service_q not in SERVICES:
            raise HTTPException(403, "dumb http not supported")
    elif rest not in SERVICES:
        raise HTTPException(404)
    if required_scope(rest, service_q) == "write" and not p.can_write:
        raise HTTPException(403, "push requires a write token")

    body = await request.body()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_PROJECT_ROOT": str(v.root.parent),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": f"/{v.root.name}/{rest}",
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": request.url.query,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": str(len(body)),
        "REMOTE_USER": p.actor,
        "REMOTE_ADDR": request.client.host if request.client else "",
        "GIT_COMMITTER_NAME": p.actor,
        "GIT_COMMITTER_EMAIL": "git@forest",
    }
    if request.headers.get("content-encoding"):
        env["HTTP_CONTENT_ENCODING"] = request.headers["content-encoding"]
    if request.headers.get("git-protocol"):
        env["GIT_PROTOCOL"] = request.headers["git-protocol"]

    def run():
        if rest == "git-receive-pack":
            with v.lock:  # don't let a push race with API writes
                return subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env)
        return subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env)

    r = await run_in_threadpool(run)
    out = r.stdout
    head, _, payload = out.partition(b"\r\n\r\n")
    if not _:
        head, _, payload = out.partition(b"\n\n")
    status, headers = 200, {}
    for line in head.decode(errors="replace").splitlines():
        if ":" not in line:
            continue
        k, val = line.split(":", 1)
        if k.lower() == "status":
            status = int(val.strip().split()[0])
        else:
            headers[k.strip()] = val.strip()
    if rest == "git-receive-pack":
        auth.audit("git.push", vault=v.name, status=status)
    return Response(payload, status_code=status, headers=headers)
