"""HTTPS/JSON client: bearer auth, idempotency, 401 renew, 409, backoff."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from harness.fence import Fence


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> "TransportResponse": ...


class TransportResponse:
    def __init__(self, status_code: int, body: Any, text: str) -> None:
        self.status_code = status_code
        self.body = body
        self.text = text


class UrllibTransport:
    def __init__(self, base_url: str, timeout_s: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> TransportResponse:
        payload = None
        merged = dict(headers or {})
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            merged.setdefault("Content-Type", "application/json")
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            headers=merged,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                text = response.read().decode("utf-8")
                return _parse(response.status, text)
        except HTTPError as exc:
            text = exc.read().decode("utf-8")
            return _parse(exc.code, text)
        except URLError as exc:
            raise TransientError(0, str(exc.reason), path) from exc


def _parse(status: int, text: str) -> TransportResponse:
    try:
        body = json.loads(text) if text else None
    except json.JSONDecodeError:
        body = None
    return TransportResponse(status, body, text)


class BenchmarkError(RuntimeError):
    def __init__(self, status_code: int, detail: Any, path: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.path = path
        super().__init__(f"{path} failed with {status_code}: {detail}")


class ConflictError(BenchmarkError):
    """409: treat as confirmation of the idempotent mutation, then refresh."""


class TransientError(BenchmarkError):
    pass


class BenchmarkClient:
    def __init__(
        self,
        transport: Transport,
        *,
        fence: Fence,
        get_access_token: Callable[[], str | None],
        reauthenticate: Callable[[], None],
        retries: dict[str, float] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport
        self.fence = fence
        self._get_access_token = get_access_token
        self._reauthenticate = reauthenticate
        retries = retries or {}
        self.base_s = float(retries.get("base_s", 2))
        self.cap_s = float(retries.get("cap_s", 60))
        self.max_attempts = int(retries.get("max_attempts", 6))
        self._sleep = sleeper

    def create_challenge(self, wallet_address: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/auth/challenges",
            auth=False,
            json_body={
                "wallet_namespace": "eip155",
                "chain_id": "8453",
                "wallet_address": wallet_address,
                "benchmark_version": "business-bench-saas-v0",
            },
            expected_status=201,
        )

    def verify_challenge(
        self, *, challenge_id: str, wallet_address: str, signature: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/auth/verify",
            auth=False,
            json_body={
                "challenge_id": challenge_id,
                "wallet_address": wallet_address,
                "signature": signature,
            },
        )

    def create_run(
        self,
        *,
        idempotency_key: str,
        participant_manifest: dict[str, Any],
        scenario_id: str,
        track: str = "practice",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/runs",
            json_body={
                "benchmark_version": "business-bench-saas-v0",
                "track": track,
                "scenario_id": scenario_id,
                "participant_manifest": participant_manifest,
            },
            idempotency_key=idempotency_key,
            expected_status=201,
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def list_runs(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        query: list[str] = []
        if limit is not None:
            query.append(f"limit={int(limit)}")
        if offset:
            query.append(f"offset={int(offset)}")
        path = "/v1/runs"
        if query:
            path = f"{path}?{'&'.join(query)}"
        return self._request("GET", path)

    def cancel_run(
        self, run_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/cancel",
            idempotency_key=idempotency_key,
        )

    def get_observation(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/observation")

    def get_tools(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/tools")

    def execute_actions(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        sequence: int,
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        for action in actions:
            self.fence.check(str(action["tool"]))
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/actions",
            json_body={"sequence": sequence, "actions": actions},
            idempotency_key=idempotency_key,
        )

    def advance(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        sequence: int,
        rationale: str,
        forecasts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/runs/{run_id}/advance",
            json_body={
                "sequence": sequence,
                "rationale": rationale,
                "forecasts": forecasts,
            },
            idempotency_key=idempotency_key,
        )

    def get_score(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/score")

    def get_trajectory(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/trajectory")

    def create_ticket(
        self,
        *,
        idempotency_key: str,
        kind: str,
        title: str,
        body: str,
        severity: str = "medium",
        run_id: str | None = None,
        phase: str | None = None,
        tags: list[str] | None = None,
        score_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": kind,
            "severity": severity,
            "title": title,
            "body": body,
        }
        if run_id is not None:
            payload["run_id"] = run_id
        if phase is not None:
            payload["phase"] = phase
        if tags:
            payload["tags"] = tags
        if score_snapshot is not None:
            payload["score_snapshot"] = score_snapshot
        return self._request(
            "POST",
            "/v1/tickets",
            json_body=payload,
            idempotency_key=idempotency_key,
            expected_status=201,
        )

    def list_tickets(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        query = {
            key: value
            for key, value in {
                "kind": kind,
                "status": status,
                "run_id": run_id,
                "limit": limit,
                "offset": offset,
            }.items()
            if value is not None
        }
        path = "/v1/tickets"
        if query:
            path = f"{path}?{urlencode(query)}"
        return self._request("GET", path)

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/tickets/{ticket_id}")

    def list_run_tickets(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}/tickets")

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_status: int | None = None,
    ) -> dict[str, Any]:
        reauthed = False
        attempt = 0
        while True:
            headers: dict[str, str] = {}
            if auth:
                token = self._get_access_token()
                if not token:
                    self._reauthenticate()
                    token = self._get_access_token()
                if not token:
                    raise RuntimeError("authentication did not produce an access token")
                headers["Authorization"] = f"Bearer {token}"
            if idempotency_key is not None:
                headers["Idempotency-Key"] = idempotency_key
            response = self.transport.request(
                method, path, headers=headers, json_body=json_body
            )
            if response.status_code == 401 and auth and not reauthed:
                self._reauthenticate()
                reauthed = True
                continue
            if response.status_code in {429, 502, 503, 504} and attempt < self.max_attempts:
                delay = min(self.cap_s, self.base_s * (2**attempt))
                self._sleep(delay)
                attempt += 1
                continue
            detail = (
                response.body.get("detail")
                if isinstance(response.body, dict)
                else response.text
            )
            if response.status_code == 409:
                raise ConflictError(409, detail, path)
            if expected_status is not None:
                ok = response.status_code == expected_status
            else:
                ok = 200 <= response.status_code < 300
            if not ok:
                raise BenchmarkError(response.status_code, detail, path)
            if not isinstance(response.body, dict):
                raise BenchmarkError(response.status_code, "expected_json_object", path)
            return response.body
