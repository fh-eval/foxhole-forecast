from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any


@dataclass
class ApiResult:
    data: Any
    etag: str | None
    not_modified: bool = False


class WarApiClient:
    def __init__(self, base_url: str, timeout: int = 20, workers: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.workers = workers

    def get(self, path: str, etag: str | None = None) -> ApiResult:
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={
                "Accept": "application/json",
                "User-Agent": "FoxholeForecast/0.1 (+https://github.com/)",
                **({"If-None-Match": etag} if etag else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ApiResult(
                    data=json.loads(response.read().decode("utf-8")),
                    etag=response.headers.get("ETag"),
                )
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return ApiResult(data=None, etag=etag, not_modified=True)
            raise

    def get_with_retry(self, path: str, etag: str | None = None) -> ApiResult:
        last_error: Exception | None = None
        for delay in (0, 1, 3):
            if delay:
                time.sleep(delay)
            try:
                return self.get(path, etag)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
        assert last_error is not None
        raise last_error

    def fetch_many(self, requests: list[tuple[str, str, str | None]]) -> dict[str, ApiResult]:
        results: dict[str, ApiResult] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.get_with_retry, path, etag): key
                for key, path, etag in requests
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

