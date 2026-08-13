# ViperCapture TypeScript SDK

This typed beta client uses `fetch` in Node.js and same-origin browser
applications.
Cross-origin browser use requires the deployment's trusted gateway to provide a
restricted CORS policy; the public-beta Compose stack does not enable CORS.

```ts
import {ViperCapture} from "@vipercapture/sdk";

const client = new ViperCapture("http://127.0.0.1:8000");
const png = await client.render({url: "https://example.com", output: "png"});
```

Pass a project API key as the second constructor argument for authenticated
deployments.
