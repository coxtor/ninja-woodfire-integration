"""I-14: the credential-extraction helper (no adb, no real keys involved)."""
from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/extract_credentials.py"


def _module():
    spec = importlib.util.spec_from_file_location("extract_credentials", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract_credentials = _module()


# ------------------------------------------------------------------ capture


def _fake_child(monkeypatch, body: str):
    """Replace the adb calls with a synthetic Python child process."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    original = subprocess.Popen

    def popen(_args, **kwargs):
        return original([sys.executable, "-c", body], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


def test_a_silent_child_does_not_outlive_the_deadline(monkeypatch):
    _fake_child(monkeypatch, "import time; time.sleep(5)")
    started = time.monotonic()
    assert extract_credentials.capture_logcat(0.2) == ""
    assert time.monotonic() - started < 2.0


def test_output_up_to_the_deadline_is_collected(monkeypatch):
    _fake_child(
        monkeypatch,
        "import sys, time\n"
        "print('first', flush=True)\n"
        "time.sleep(0.05)\n"
        "print('second', flush=True)\n"
        "sys.stdout.write('partial')\n"
        "sys.stdout.flush()\n"
        "time.sleep(5)\n",
    )
    started = time.monotonic()
    log = extract_credentials.capture_logcat(0.5)
    assert time.monotonic() - started < 2.0
    assert "first" in log and "second" in log


# ------------------------------------------------------------------ parsing


def _jwt(audience: str, client_id: str) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"aud": [audience, "https://example.invalid/userinfo"], "azp": client_id}
    return f"{part({'alg': 'RS256'})}.{part(claims)}.sig"


def _block(region: str, suffix: str) -> str:
    return (
        f", {region}: ComboSessionParameters {{ app_info: ApplicationInfo {{ "
        f'app_id: "app-{suffix}", app_secret: "secret-{suffix}" }}, '
        f'okta_info: Some(OktaInfo {{ client_id: "client-{suffix}" }}) }}'
    )


EU_AUD = "https://tenant-eu.eu.auth0.test/api/v2/"
NA_AUD = "https://tenant-na.us.auth0.test/api/v2/"


def test_a_single_region_log_parses():
    log = _block("EU", "eu") + "\n" + _jwt(EU_AUD, "client-eu")
    creds = extract_credentials.extract(log, "EU")
    assert creds["ayla_app_id"] == "app-eu"
    assert creds["auth0_client_id"] == "client-eu"
    assert creds["auth0_audience"] == EU_AUD


def test_a_mixed_log_picks_the_audience_of_the_requested_region():
    log = "\n".join([
        _block("EU", "eu"),
        _block("NA", "na"),
        _jwt(EU_AUD, "client-eu"),
        _jwt(NA_AUD, "client-na"),
    ])
    creds = extract_credentials.extract(log, "NA")
    assert creds["auth0_client_id"] == "client-na"
    assert creds["auth0_audience"] == NA_AUD


def test_a_single_foreign_jwt_is_not_used_for_another_region():
    # Only the EU token was captured; pairing its audience with the NA client
    # id yields a config that cannot log in.
    log = "\n".join([_block("EU", "eu"), _block("NA", "na"), _jwt(EU_AUD, "client-eu")])
    with pytest.raises(SystemExit, match="another region"):
        extract_credentials.extract(log, "NA")


def test_a_jwt_without_a_client_binding_is_still_used():
    token = _jwt(EU_AUD, "client-eu")
    header, claims, sig = token.split(".")
    unbound = json.loads(base64.urlsafe_b64decode(claims + "=="))
    unbound.pop("azp")
    payload = base64.urlsafe_b64encode(json.dumps(unbound).encode()).rstrip(b"=").decode()
    log = _block("EU", "eu") + "\n" + f"{header}.{payload}.{sig}"
    assert extract_credentials.extract(log, "EU")["auth0_audience"] == EU_AUD


def test_an_uncorrelatable_mixed_log_is_rejected():
    # Two audiences, neither tied to the chosen region's client id: guessing
    # would silently produce a config that cannot log in.
    log = "\n".join([
        _block("EU", "eu"),
        _block("NA", "na"),
        _jwt(EU_AUD, "someone-else"),
        _jwt(NA_AUD, "someone-else"),
    ])
    with pytest.raises(SystemExit, match="more than one region"):
        extract_credentials.extract(log, "NA")
