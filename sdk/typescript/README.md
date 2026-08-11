# ViperCapture TypeScript SDK

Typed beta client for browser and Node.js runtimes with `fetch`.

```ts
import {ViperCapture} from "@vipercapture/sdk";

const client = new ViperCapture("http://127.0.0.1:8000");
const png = await client.render({url: "https://example.com", output: "png"});
```

Pass a project API key as the second constructor argument for authenticated
deployments.
