# ViperCapture Python SDK

This beta client connects to a self-hosted ViperCapture API and has no runtime
dependencies.

```python
from vipercapture import Client

client = Client("http://127.0.0.1:8000")
png = client.render({"url": "https://example.com", "output": "png"})
open("example.png", "wb").write(png)
```

Pass a project API key as `api_key=` for authenticated deployments. The client
rejects API redirects so credentials and render inputs stay on the configured
origin.
