"""Spawns one isolated, VNC-viewable/controllable browser container per run.

Adapted from Project-Hawkeye's `hawkeye_sandbox` module, trimmed down deliberately:
no multi-browser spawn_all, no MCP config generation, no mp4 recording - just
"start a container, get a CDP URL to drive it and a noVNC URL to watch/control it,
stop the container when the run is done." The container image itself
(`sandbox_image/`) is copied from Hawkeye with one change: x11vnc's `-viewonly` flag
is removed, since escalation (§3.6) requires a human to actually take control of the
live session, not just watch it.
"""
from __future__ import annotations

import dataclasses
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional


@dataclasses.dataclass(frozen=True)
class SandboxConfig:
    url: str
    width: int = 1366
    height: int = 768

    image: str = "comp-use-sandbox:latest"

    # Container-internal ports (fixed inside the image - see sandbox_image/supervisord.conf).
    novnc_port: int = 6080
    cdp_proxy_port: int = 9223

    host: str = "127.0.0.1"
    scheme: str = "http"

    name_prefix: str = "comp-use-sandbox-run"
    shm_size: str = "1g"


@dataclasses.dataclass(frozen=True)
class SandboxHandle:
    container_id: str
    container_name: str
    novnc_url: str
    cdp_url: str


class SandboxError(Exception):
    pass


def spawn(cfg: SandboxConfig, *, timeout_s: int = 45) -> SandboxHandle:
    """Start one sandbox container running a headed Chromium already navigated to
    cfg.url. Blocks until the container's ports are published and reachable."""
    run_id = uuid.uuid4().hex[:10]
    name = f"{cfg.name_prefix}-{run_id}"

    cmd = [
        "docker", "run", "-d", "--rm", "--name", name, "--shm-size", cfg.shm_size,
        "-e", "DISPLAY=:99",
        "-e", f"SCREEN_WIDTH={cfg.width}",
        "-e", f"SCREEN_HEIGHT={cfg.height}",
        "-e", "SCREEN_DEPTH=24",
        "-e", f"CHROME_URL={cfg.url}",
        "-e", "BROWSER=chromium",
        "-e", "CHROME_CDP_PORT=9222",
        "-p", f"{cfg.host}::{cfg.novnc_port}",
        "-p", f"{cfg.host}::{cfg.cdp_proxy_port}",
        cfg.image,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SandboxError(f"docker run failed: {result.stderr.strip()}")
    container_id = result.stdout.strip()

    deadline = time.time() + timeout_s
    novnc_url: Optional[str] = None
    cdp_url: Optional[str] = None
    while time.time() < deadline:
        port_out = subprocess.run(["docker", "port", name], capture_output=True, text=True).stdout
        for line in port_out.splitlines():
            if line.startswith(f"{cfg.novnc_port}/tcp ->"):
                novnc_url = f"{cfg.scheme}://{cfg.host}:{line.rsplit(':', 1)[-1].strip()}/"
            if line.startswith(f"{cfg.cdp_proxy_port}/tcp ->"):
                cdp_url = f"{cfg.scheme}://{cfg.host}:{line.rsplit(':', 1)[-1].strip()}"
        if novnc_url and cdp_url:
            break
        time.sleep(0.2)

    if not novnc_url or not cdp_url:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        raise SandboxError(f"sandbox '{name}' started, but ports were not published within {timeout_s}s")

    # A published port only means Docker is forwarding it - not that Chromium (started
    # by supervisord's "browser" program, after xvfb/xfce) is actually listening on the
    # other end yet. Connecting to connect_over_cdp() too early fails with a bare
    # "socket hang up" (observed live) rather than a clear "not ready" error. Poll the
    # CDP HTTP endpoint itself until it actually answers.
    cdp_deadline = time.time() + timeout_s
    cdp_ready = False
    while time.time() < cdp_deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as resp:
                if resp.status == 200:
                    cdp_ready = True
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.3)

    if not cdp_ready:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        raise SandboxError(f"sandbox '{name}' published its CDP port, but Chromium never answered within {timeout_s}s")

    return SandboxHandle(container_id=container_id, container_name=name, novnc_url=novnc_url, cdp_url=cdp_url)


def stop(handle: SandboxHandle) -> None:
    subprocess.run(["docker", "rm", "-f", handle.container_id], capture_output=True)
