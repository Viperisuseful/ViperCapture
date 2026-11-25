# Authorize ViperCapture through Cloudflare or a WAF

Use this guide when you control a site and want to let ViperCapture capture it without disabling your WAF. ViperCapture detects blocking challenges and can capture the challenge page as shown, but it does not solve CAPTCHAs or evade another site's access controls.

This repository uses standard Playwright and does not claim a Cloudflare bypass. For Patchright and harder Turnstile challenge handling, see [ViperCapture Stealth](https://github.com/Viperisuseful/ViperCapture-Stealth) (`0.1.0-beta`). Stealth is not a universal Cloudflare bypass.

## Create an access rule

Set up a dedicated preview hostname or path and require a long, random, revocable request header. Match both that header and the fixed outbound IP address of your ViperCapture deployment. Skip only the specific security rule that blocks the capture, and leave logging and all other protections enabled.

Send an origin-scoped header with your render request:

```bash
curl http://127.0.0.1:8000/v1/render \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://www.example.com/capture-preview/report",
    "output": "png",
    "headers": {
      "X-ViperCapture-Key": "replace-with-a-long-random-value"
    }
  }' --output report.png
```

ViperCapture sends caller-supplied headers only to the exact origin of `url`. Redirects and cross-origin assets never receive the secret. Use persistent profiles for short-lived login sessions instead of copying a person's long-lived session into a request.

## Hand off from an external challenge tool

For a target you are authorized to test, a caller can complete the site's access flow outside ViperCapture with its own tool and then start a fresh render. Pass only short-lived, target-scoped state through an encrypted profile, `network.cookies`, or an exact-origin header. Keep any external service's API key in the caller or operator integration, never in target cookies, headers, profiles, or render payloads.

This caller-managed handoff is separate from the optional operator handler in the [platform guide](platform.md). ViperCapture is not affiliated with external providers and does not bundle, call, endorse, or configure one. It does not solve or bypass challenges. Same-origin header routing stays active, and hosted mode keeps its target-domain check for `network.cookies` along with public-address and redirect validation. Because imported profile state is not target-filtered, create a dedicated profile holding only the authorized site's state. Self-hosters must still block private and metadata networks with an egress policy.

## Configure Cloudflare

Add a WAF custom rule above the rule that blocks the renderer. Replace the IP address, hostname, path prefix, and header secret below:

```text
(
  ip.src eq 203.0.113.10 and
  http.host eq "www.example.com" and
  starts_with(http.request.uri.path, "/capture-preview/") and
  any(http.request.headers["x-vipercapture-key"][*] eq "replace-with-a-long-random-value")
)
```

Select **Skip** and pick only the managed rule, bot rule, or rate limit that triggers on the capture. Never disable the WAF globally. Treat a broad IP allow rule as a last resort because it cannot restrict access by path and secret header. See Cloudflare's documentation for [Skip rules](https://developers.cloudflare.com/waf/custom-rules/skip/) and [request-header expressions](https://developers.cloudflare.com/ruleset-engine/rules-language/fields/reference/http.request.headers/).

Use the same least-privilege rule shape on other CDNs and WAFs: fixed renderer source IP, exact hostname, dedicated path, and a secret header. Check origin-side rate limits and security middleware as well.

## Troubleshooting

- **HTTP 403 or Cloudflare error 1020.** Check the provider security event log for the exact rule ID that matched.
- **HTTP 429.** Exempt only this narrow rule from the edge or origin rate limit.
- **`captcha_detected`.** Remove the challenge from the authorized rule. Setting `proceed_on_captcha: true` captures the challenge page as displayed and does not solve it. Harder Turnstile handling lives in the [Stealth fork](https://github.com/Viperisuseful/ViperCapture-Stealth), not this engine, and is not a universal bypass.
- **Missing fonts or images.** Inspect the diagnostic bundle for blocked cross-origin assets, and authorize an asset host only if you control it.

Verify the first capture in both edge and origin logs, confirm that requests without the secret header stay blocked, rotate the secret regularly, and remove the rule when you no longer need it.

## Related

- [API and workflows](api.md)
- [Projects, API keys, and observability](platform.md)
- [Self-hosting and local setup](self-hosting.md)
- [All docs](README.md)
