#!/usr/bin/env python3
"""Capture the Ninja-Kitchen app's region credentials via adb logcat.

Run this once before installing the integration. It launches `adb logcat`,
asks you to open the Ninja-Kitchen app on your phone, then writes the
extracted values to a TXT file you paste into Home Assistant during the
config flow.

Usage:
    python3 extract_credentials.py [--region EU|NA] [--output creds.txt]

Requirements:
    - Android phone with USB debugging enabled
    - The official "Ninja Kitchen" app installed and a Ninja account
    - `adb` on your PATH (Android Platform Tools)
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# The app dumps a single-line Rust Debug of all known regions on startup,
# e.g.:
#   ..., EU: ComboSessionParameters { app_info: ApplicationInfo {
#     app_id: "...", app_secret: "..." },
#     device_url: "...", user_url: "...",
#     okta_info: Some(OktaInfo { client_id: "..." }),
#     aws_info: ... }
#
# We anchor on `, <REGION>: ComboSessionParameters` (with a leading
# space-comma) so EU doesn't match inside EUDev. We grab the first
# app_id, app_secret, and client_id that follow.
REGION_BLOCK = re.compile(
    r"[ ,](?P<region>EU|NA): ComboSessionParameters \{.*?"
    r'app_id: "(?P<app_id>[^"]+)".*?'
    r'app_secret: "(?P<app_secret>[^"]+)".*?'
    r'client_id: "(?P<client_id>[^"]+)"',
    re.DOTALL,
)

# Audience isn't logged in plain text — it's encoded inside the JWTs the
# app receives. We grab any access_token / id_token from the log, base64
# the payload portion, and read the `aud` claim.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def decode_jwt_audience(token: str) -> str | None:
    """Return the api/v2/ audience URL from a JWT, or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    aud = claims.get("aud")
    if isinstance(aud, str) and "/api/v2/" in aud:
        return aud
    if isinstance(aud, list):
        for a in aud:
            if isinstance(a, str) and "/api/v2/" in a:
                return a
    return None


def find_audience(log: str) -> str | None:
    for token in JWT.findall(log):
        aud = decode_jwt_audience(token)
        if aud:
            return aud
    return None


def run_adb(args: list[str]) -> str:
    return subprocess.check_output(["adb", *args], text=True, errors="replace")


def check_adb_device() -> None:
    try:
        out = run_adb(["devices"])
    except FileNotFoundError:
        sys.exit("error: `adb` not found on PATH. Install Android Platform Tools.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: adb failed: {e}")
    lines = [ln for ln in out.splitlines() if "\tdevice" in ln]
    if not lines:
        sys.exit(
            "error: no Android device authorised over adb.\n"
            "  - plug your phone in via USB\n"
            "  - enable Developer Options → USB debugging\n"
            "  - confirm the 'Allow USB debugging?' prompt on the phone"
        )
    print(f"adb: found device {lines[0].split()[0]}")


def capture_logcat(seconds: int) -> str:
    print(f"adb: clearing log buffer")
    subprocess.run(["adb", "logcat", "-c"], check=False)
    print(f"adb: capturing for {seconds}s — open the Ninja Kitchen app NOW")
    print(f"     (force-stop first, then open and let the device list load)")
    proc = subprocess.Popen(
        ["adb", "logcat", "-v", "raw", "*:V"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    deadline = time.time() + seconds
    chunks: list[str] = []
    try:
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    break
                continue
            chunks.append(line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return "".join(chunks)


def extract(log: str, region: str) -> dict[str, str]:
    found: dict[str, dict[str, str]] = {}
    for m in REGION_BLOCK.finditer(log):
        found[m.group("region")] = {
            "ayla_app_id": m.group("app_id"),
            "ayla_app_secret": m.group("app_secret"),
            "auth0_client_id": m.group("client_id"),
        }
    if region not in found:
        regions = ", ".join(sorted(found)) or "none"
        raise SystemExit(
            f"error: region {region!r} not found in log (saw: {regions}).\n"
            "  - confirm the app actually opened during the capture window\n"
            "  - try increasing --seconds (default 30)"
        )
    aud = find_audience(log)
    if not aud:
        raise SystemExit(
            "error: auth0 audience not found in log.\n"
            "  - log in to the app during the capture so a fresh JWT is fetched\n"
            "  - if already logged in, log out and back in"
        )
    creds = found[region]
    creds["auth0_audience"] = aud
    creds["region"] = region
    return creds


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--region", choices=("EU", "NA"), default="EU",
                   help="which region's credentials to extract (default EU)")
    p.add_argument("--output", default="ninja_woodfire_credentials.txt",
                   help="where to write the result (default %(default)s)")
    p.add_argument("--seconds", type=int, default=30,
                   help="how long to capture logcat (default 30)")
    p.add_argument("--from-file",
                   help="parse this existing logcat file instead of capturing")
    args = p.parse_args()

    if args.from_file:
        log = Path(args.from_file).read_text(errors="replace")
    else:
        check_adb_device()
        log = capture_logcat(args.seconds)
        if not log.strip():
            sys.exit("error: empty logcat output. Is the device still connected?")

    creds = extract(log, args.region)

    out_path = Path(args.output)
    body = (
        "# Ninja Woodfire credentials — paste these into the integration's\n"
        "# config flow. Treat this file as a secret; do not commit to git.\n"
        f"region          {creds['region']}\n"
        f"auth0_audience  {creds['auth0_audience']}\n"
        f"auth0_client_id {creds['auth0_client_id']}\n"
        f"ayla_app_id     {creds['ayla_app_id']}\n"
        f"ayla_app_secret {creds['ayla_app_secret']}\n"
    )
    out_path.write_text(body)
    print()
    print(f"wrote {out_path.resolve()}")
    print()
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
