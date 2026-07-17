"""Local smoke test for the open-source v1 render route."""

import json
from pathlib import Path
import time
import urllib.request


def take_screenshot(url: str, filename: str) -> None:
    request = urllib.request.Request(
        "http://127.0.0.1:8000/v1/render",
        data=json.dumps(
            {
                "url": url,
                "output": "png",
                "viewport": {"width": 640, "height": 480, "device_scale_factor": 1},
                "full_page": False,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"Requesting screenshot for {url}...")
    with urllib.request.urlopen(request, timeout=90) as response:
        image = response.read()
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) < 500:
        raise RuntimeError("ViperCapture returned an invalid PNG")
    Path(filename).write_bytes(image)
    print(f"Saved {filename}")


time.sleep(3)
take_screenshot("https://example.com", "capture-smoke.png")
