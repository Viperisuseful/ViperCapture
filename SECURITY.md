# Security Policy

## Supported code

This policy covers the open-source ViperCapture engine in this repository. It
does not cover the hosted ViperCapture service, which uses separate code and
infrastructure.

Only the latest code on `master` is supported. Older commits, archived releases,
third-party forks, and modified deployments are not supported.

## Report a vulnerability

Do not publish vulnerability details in an issue, discussion, or pull request.
Use GitHub's private vulnerability reporting:

1. Open this repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

Security contact: viperisuseful@gmail.com

If that option is unavailable, email viperisuseful@gmail.com, and don't
include technical details in public channels.

Include the affected component, reproduction steps, a minimal proof of concept,
impact, and any suggested mitigation. Remove secrets, credentials, cookies,
private URLs, and other people's data.

Relevant reports include bypasses of URL or redirect validation, DNS rebinding,
private-network access, unsafe header forwarding, arbitrary file access,
command execution, and browser isolation failures.

## Out of scope

- The hosted ViperCapture service
- Third-party sites being captured
- Unofficial forks or modified deployments
- Social engineering, spam, or denial-of-service testing
- Dependency-version reports without a reproducible impact
- Automated findings without a reproducible security issue

## Safe harbor

Good-faith research against code and systems you own or are authorized to test
will be treated as authorized when it avoids privacy violations, data access,
and service disruption. Stop and report privately if sensitive information is
encountered, and allow reasonable time for investigation before disclosure.

ViperCapture does not operate a paid bug bounty program.
