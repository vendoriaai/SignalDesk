"""Update check (roadmap Phase 4.24): tiny update manifest polled at app start.

v1 policy: manual download — the UI shows a notice with a link to the newest
installer. TUF-style auto-update is planned for 1.1. Manifest URL is
configurable via SIGNALDESK_UPDATE_URL; default points at the project's
raw GitHub file (published by the release workflow).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

import signaldesk

DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/signaldesk/signaldesk/main/update-manifest.json"
)


@dataclass
class UpdateInfo:
    current: str
    latest: str
    update_available: bool
    url: str         # download page
    notes: str = ""


def _parse_version(v: str) -> tuple[int, ...]:
    import re

    parts = []
    for chunk in v.lstrip("v").split("."):
        m = re.match(r"\d+", chunk)  # leading digits only: "0b1" -> 0 (beta of 1.2.0)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def check_for_update(manifest_url: str | None = None, timeout: float = 6.0) -> UpdateInfo:
    url = manifest_url or os.environ.get("SIGNALDESK_UPDATE_URL") or DEFAULT_MANIFEST_URL
    current = signaldesk.__version__
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        latest = str(data.get("version", "")).lstrip("v")
        if not latest:
            raise ValueError("manifest missing 'version'")
        return UpdateInfo(
            current=current,
            latest=latest,
            update_available=_parse_version(latest) > _parse_version(current),
            url=data.get("url", "https://github.com/signaldesk/signaldesk/releases"),
            notes=str(data.get("notes", ""))[:500],
        )
    except Exception:
        # unreachable manifest is normal offline; never fail the app for it
        return UpdateInfo(current=current, latest=current, update_available=False, url="")
