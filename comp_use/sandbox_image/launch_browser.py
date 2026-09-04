from __future__ import annotations

import json
import os
import time

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError

_PROFILE_DIR = "/home/pwuser/browser-profile"


def _get_env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v else default


def _disable_password_manager_prefs() -> None:
    """--disable-features=PasswordLeakDetection (in the launch_args below) targets
    the experimental flag path, but the "this password was found in a data breach"
    dialog observed live obstructing the noVNC feed kept firing anyway - it's driven
    by these two profile-level Preferences instead. Chromium creates the profile
    directory itself on first launch, but nothing stops us from seeding it first:
    Chromium merges this file with its own defaults on startup rather than
    overwriting it."""
    default_dir = os.path.join(_PROFILE_DIR, "Default")
    os.makedirs(default_dir, exist_ok=True)
    prefs_path = os.path.join(default_dir, "Preferences")
    prefs: dict = {}
    if os.path.exists(prefs_path):
        try:
            with open(prefs_path, encoding="utf-8") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, OSError):
            prefs = {}
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})
    prefs["profile"]["password_manager_leak_detection"] = False
    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f)


def main() -> int:
    os.environ["DISPLAY"] = _get_env("DISPLAY", ":99")

    url = _get_env("CHROME_URL", "about:blank")
    browser_name = _get_env("BROWSER", "chromium").lower()

    width = int(_get_env("SCREEN_WIDTH", "1366"))
    height = int(_get_env("SCREEN_HEIGHT", "768"))
    chromium_cdp_port = int(_get_env("CHROME_CDP_PORT", "9222"))

    if browser_name in ("chromium", "chrome", "msedge", "edge"):
        _disable_password_manager_prefs()

    with sync_playwright() as p:
        if browser_name in ("chromium", "chrome", "msedge", "edge"):
            browser_type = p.chromium
            channel = None
            if browser_name in ("msedge", "edge"):
                channel = "msedge"
            elif browser_name == "chrome":
                channel = "chrome"
            launch_args = [
                f"--window-size={width},{height}",
                "--remote-debugging-address=0.0.0.0",
                f"--remote-debugging-port={chromium_cdp_port}",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-breakpad",
                "--disable-gpu",
                "--no-restore-last-session",
                "--restore-last-session=false",
                "--remote-allow-origins=*",
                # MERIDIAN CORE's login form (a real-looking username/password pair
                # typed repeatedly across discovery/replay runs) trips Chrome's
                # built-in "this password was found in a data breach" leak-detection
                # dialog, which sits on top of the page and blocks the noVNC viewer
                # until someone clicks OK - observed live obstructing the dashboard's
                # live feed. PasswordLeakDetection is the feature behind that dialog;
                # AutofillServerCommunication/Translate/OptimizationHints are the
                # same category of unsolicited-popup features, disabled for the same
                # reason (nothing in this sandbox should ever need a human to
                # dismiss something that wasn't part of the recorded/replayed steps).
                "--disable-features=PasswordLeakDetection,AutofillServerCommunication,Translate,OptimizationHints",
                "--disable-save-password-bubble",
                "--disable-notifications",
                "--password-store=basic",
            ]
        elif browser_name == "firefox":
            browser_type = p.firefox
            launch_args = []
            channel = None
        elif browser_name == "webkit":
            browser_type = p.webkit
            launch_args = []
            channel = None
        else:
            raise SystemExit(
                f"Unsupported BROWSER={browser_name!r} "
                f"(expected chromium|chrome|msedge|firefox|webkit)"
            )

        try:
            context = browser_type.launch_persistent_context(
                user_data_dir="/home/pwuser/browser-profile",
                headless=False,
                args=launch_args,
                channel=channel,
                viewport={"width": width, "height": height},
            )
        except PlaywrightError as e:
            # On Linux Arm64, Playwright does not support chrome/msedge channels.
            # Fall back to bundled Chromium when a channel binary is missing.
            if channel and "is not found" in str(e).lower():
                context = browser_type.launch_persistent_context(
                    user_data_dir="/home/pwuser/browser-profile",
                    headless=False,
                    args=launch_args,
                    channel=None,
                    viewport={"width": width, "height": height},
                )
            else:
                raise
        # Reuse the page launch_persistent_context already opens; never create a second tab.
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        while True:
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())

