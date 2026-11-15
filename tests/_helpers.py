"""测试共用小工具。"""
from __future__ import annotations

import time


def wait_phase(client, target: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while True:
        data = client.get("/migration/progress").json()
        if data["state"] == target:
            return data
        assert time.time() < deadline, f"phase stuck at {data['state']}, want {target}"
        time.sleep(0.2)
