"""Exercise a running local or Railway API without printing bearer credentials."""

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4


class Demo:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def call(self, method, path, body=None, *, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {os.environ['OFFLINE_SYNC_DEMO_TOKEN']}"
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            payload = json.load(response)
            response_headers = dict(response.headers)
        if status != expected:
            raise RuntimeError(f"{method} {path}: expected {expected}, got {status}")
        return payload, {key.lower(): value for key, value in response_headers.items()}

    def run(self):
        self.call("GET", "/healthz", authenticated=False)
        account, _ = self.call(
            "POST", "/v1/users", {"name": "Sync demonstration"}, expected=201, authenticated=False
        )
        os.environ["OFFLINE_SYNC_DEMO_TOKEN"] = account.pop("access_token")
        laptop, phone, document_id = str(uuid4()), str(uuid4()), str(uuid4())
        for device_id, name in ((laptop, "Laptop"), (phone, "Phone")):
            self.call("PUT", f"/v1/devices/{device_id}", {"name": name})

        def edit(base, changes, device):
            return {
                "request_id": str(uuid4()),
                "device_id": device,
                "document_id": document_id,
                "base_version": base,
                "changes": changes,
            }

        creation = edit(0, {"title": "Trip", "content": "Original notes"}, laptop)
        first, _ = self.call("POST", "/v1/sync", creation, expected=201)
        assert first["document"]["version"] == 1
        self.call("POST", "/v1/sync", edit(1, {"title": "Laptop title"}, laptop))
        merged, _ = self.call("POST", "/v1/sync", edit(1, {"content": "Phone notes"}, phone))
        assert merged["outcome"] == "merged"
        assert merged["document"]["version"] == 3
        assert merged["document"]["data"]["title"] == "Laptop title"
        print("PASS: independent offline fields merged without losing the laptop edit")

        proposal = edit(1, {"title": "Phone title"}, phone)
        conflict, _ = self.call("POST", "/v1/sync", proposal, expected=409)
        conflict_id = conflict["conflict_id"]
        assert conflict["outcome"] == "conflict"
        reviewed, _ = self.call("GET", f"/v1/conflicts/{conflict_id}")
        resolved, _ = self.call(
            "POST",
            f"/v1/conflicts/{conflict_id}/resolve",
            {
                "request_id": str(uuid4()),
                "device_id": phone,
                "expected_version": reviewed["current_document"]["version"],
                "resolutions": {"title": "client"},
            },
        )
        assert resolved["document"]["version"] == 4
        assert resolved["document"]["data"]["content"] == "Phone notes"
        print("PASS: same-field conflict saved and explicitly resolved")

        replayed, headers = self.call("POST", "/v1/sync", proposal, expected=409)
        assert replayed == conflict
        assert headers["idempotency-replayed"] == "true"
        replayed, headers = self.call("POST", "/v1/sync", creation, expected=201)
        assert replayed == first
        assert headers["idempotency-replayed"] == "true"
        current, _ = self.call("GET", f"/v1/documents/{document_id}")
        assert current["version"] == 4
        print("PASS: original success/conflict responses replayed without reverting current data")

        restored, _ = self.call(
            "POST",
            f"/v1/documents/{document_id}/restore",
            {
                "request_id": str(uuid4()),
                "device_id": laptop,
                "expected_version": 4,
                "target_version": 1,
            },
        )
        assert restored["document"]["version"] == 5
        assert restored["document"]["data"] == first["document"]["data"]
        cursor, versions = 0, []
        while True:
            page, _ = self.call("GET", f"/v1/changes?after={cursor}&limit=2")
            versions.extend(item["version"] for item in page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert versions == [1, 2, 3, 4, 5]
        history, _ = self.call("GET", f"/v1/documents/{document_id}/history")
        assert len(history["items"]) == 5
        print("PASS: restore appended history; paginated pull delivered all five versions")
        print(f"Verified {self.base_url}; document {document_id}; final version 5")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
        parser.error("--base-url must be an HTTP(S) API URL without embedded credentials")
    previous = os.environ.get("OFFLINE_SYNC_DEMO_TOKEN")
    try:
        Demo(args.base_url).run()
    finally:
        if previous is None:
            os.environ.pop("OFFLINE_SYNC_DEMO_TOKEN", None)
        else:
            os.environ["OFFLINE_SYNC_DEMO_TOKEN"] = previous


if __name__ == "__main__":
    main()
