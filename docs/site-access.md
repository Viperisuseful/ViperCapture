# Authorize ViperCapture through Cloudflare or another WAF

This guide is for owners and administrators authorizing captures of a site they
control. ViperCapture detects blocking challenges and can record the page as
shown, but it does not solve CAPTCHAs or evade another site's access controls.

## Create an access rule

Create a dedicated preview hostname or path and require a random, revocable
request header. Match the header with the fixed outbound address of the
ViperCapture deployment. Skip only the security rule that blocks the request.
Keep logging and unrelated protections enabled.

For example, send an origin-scoped header with the render request:

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

Caller-supplied headers are applied only to the exact origin of `url`. Redirects
and cross-origin assets do not receive the secret. Persistent profiles are a
better fit for short-lived login sessions; do not copy a person's long-lived
session into a request.

## Hand off from an external challenge tool

For a target you are authorized to test, a caller may complete the site's
access flow outside ViperCapture with a tool of its choice and then submit a
fresh render. Pass only short-lived, target-scoped state through an encrypted
profile, `network.cookies`, or an exact-origin header. Do not put the external
service's API key in target cookies, headers, profiles, or render payloads;
keep service credentials in the caller or operator integration.

This caller-managed handoff is separate from the optional operator handler
described in the [platform guide](platform.md). ViperCapture is not affiliated
with external providers and does not bundle, call, endorse, or configure one
for this workflow. It does not solve or bypass challenges. Same-origin header
routing, hosted cookie-domain checks, redirect validation, and SSRF controls
remain in force.

## Configure Cloudflare

Create a WAF custom rule above the rule that blocks the renderer. Replace the
address, host, path, and value below:

```text
(
  ip.src eq 203.0.113.10 and
  http.host eq "www.example.com" and
  starts_with(http.request.uri.path, "/capture-preview/") and
  any(http.request.headers["x-vipercapture-key"][*] eq "replace-with-a-long-random-value")
)
```

Choose **Skip**, then select only the relevant managed rule, bot rule, or rate
limit. Do not globally disable the WAF. A broad IP allow rule is a last resort
because it cannot be limited by path and secret header. Cloudflare's current
documentation covers [Skip rules](https://developers.cloudflare.com/waf/custom-rules/skip/)
and [request-header expressions](https://developers.cloudflare.com/ruleset-engine/rules-language/fields/reference/http.request.headers/).

The same least-privilege shape applies to other CDNs and WAFs: renderer source
address, exact hostname, dedicated path, and a secret header. Check origin-side
rate limits and security middleware too.

## Troubleshooting

- For a 403 or Cloudflare error 1020, inspect the provider event and exact rule
  ID that matched.
- For a 429, exempt only this narrow integration from the relevant edge or
  origin limit.
- For `captcha_detected`, remove the challenge from the authorized rule.
  `proceed_on_captcha: true` captures the challenge as displayed; it does not
  solve it.
- For missing fonts or images, inspect the diagnostic bundle for blocked
  cross-origin assets and authorize an asset host only when you control it.

Verify the first capture in both edge and origin logs, confirm requests without
the secret remain protected, rotate the secret periodically, and remove the
exception when the integration is no longer needed.
