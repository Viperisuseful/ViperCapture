# DuckDuckGo AutoConsent

ViperCapture vendors the browser bundle and full rule bundle from
`@duckduckgo/autoconsent` 16.11.0. Upstream source:
https://github.com/duckduckgo/autoconsent

The browser bundle carries one local security hardening patch: the repeated
suffix in `ACKNOWLEDGE_PATTERNS` is bounded to three phrases to prevent
exponential regular-expression backtracking on adversarial labels. The rule
bundle is unmodified.

AutoConsent is licensed under MPL-2.0. The upstream license is included in
`LICENSE`. ViperCapture loads these assets only when a request asks for consent
cleanup; the surrounding ViperCapture source remains MIT-licensed.
