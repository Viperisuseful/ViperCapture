# DuckDuckGo AutoConsent

ViperCapture vendors the browser bundle and full rule bundle from `@duckduckgo/autoconsent` 16.11.0 ([upstream repository](https://github.com/duckduckgo/autoconsent)).

The browser bundle includes one local security patch that bounds the repeated suffix in `ACKNOWLEDGE_PATTERNS` to three phrases to prevent exponential regular-expression backtracking on adversarial labels. The rule bundle is unmodified.

AutoConsent is licensed under MPL-2.0, and `LICENSE` in this directory contains the upstream license text. ViperCapture loads these assets only when a request enables consent cleanup; the surrounding ViperCapture source stays under the MIT License.
