"""HTTP GET with timeouts, retries with backoff on 429/5xx, and a minimum gap between requests.

Copied from nem-market-explainer's ingestion/http_client.py, with streaming to disk added
because a month of BIDPEROFFER_D is 150-185 MB.
"""
from __future__ import annotations

import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = (10, 120)  # connect, read seconds


class PoliteClient:
    def __init__(self, user_agent: str, min_gap_seconds: float, retries: int = 5):
        retry = Retry(total=retries, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods={"GET"}, respect_retry_after_header=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.min_gap_seconds = min_gap_seconds
        self._last_request = 0.0
        self.requests = 0   # every request made through this client, for reporting

    def _wait(self) -> None:
        wait = self._last_request + self.min_gap_seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.requests += 1

    def get(self, url: str) -> bytes:
        self._wait()
        try:
            response = self.session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Download failed for {url}: {exc}") from exc
        finally:
            self._last_request = time.monotonic()
        return response.content

    def get_range(self, url: str, start: int, end: int) -> bytes:
        """Bytes start..end inclusive, by HTTP Range request (one polite request)."""
        self._wait()
        try:
            response = self.session.get(url, timeout=TIMEOUT, headers={"Range": f"bytes={start}-{end}"})
            response.raise_for_status()
            if response.status_code != 206:
                raise RuntimeError(f"{url} ignored the Range header (status {response.status_code})")
        except requests.RequestException as exc:
            raise RuntimeError(f"Range request failed for {url}: {exc}") from exc
        finally:
            self._last_request = time.monotonic()
        return response.content

    def size(self, url: str) -> int:
        """Content length from a HEAD request."""
        self._wait()
        try:
            response = self.session.head(url, timeout=TIMEOUT, allow_redirects=True)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"HEAD failed for {url}: {exc}") from exc
        finally:
            self._last_request = time.monotonic()
        return int(response.headers["Content-Length"])

    def download(self, url: str, target: Path) -> int:
        """Stream to target via a .part file, so an interrupted download never looks complete."""
        self._wait()
        part = target.with_suffix(target.suffix + ".part")
        size = 0
        try:
            with self.session.get(url, timeout=TIMEOUT, stream=True) as response:
                response.raise_for_status()
                with part.open("wb") as f:
                    for chunk in response.iter_content(1 << 20):
                        f.write(chunk)
                        size += len(chunk)
            part.rename(target)
        except requests.RequestException as exc:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"Download failed for {url}: {exc}") from exc
        finally:
            self._last_request = time.monotonic()
        return size
