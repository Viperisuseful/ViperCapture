# Security policy

This policy covers the open-source ViperCapture engine in this repository. It does not cover the hosted ViperCapture service, which runs separate code and infrastructure.

## Supported code

Only the latest code on `master` is supported. Older commits, archived releases, third-party forks, and modified deployments are out of support.

## Report a vulnerability

Do not disclose vulnerability details in a public issue, discussion, or pull request. Use GitHub private vulnerability reporting:

1. Open this repository's **Security** tab.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

If GitHub private reporting is unavailable, email [viperisuseful@gmail.com](mailto:viperisuseful@gmail.com) and keep technical details out of public channels.

Include the affected component, reproduction steps, a minimal proof of concept, impact, and any suggested fix. Strip secrets, credentials, cookies, private URLs, and other people's data from your report.

Relevant reports include:

- Bypasses of URL or redirect validation
- DNS rebinding or private-network access
- Unsafe header forwarding
- Arbitrary file access or command execution
- Browser isolation failures

## Out of scope

- The hosted ViperCapture service
- Third-party sites being captured
- Unofficial forks or modified deployments
- Social engineering, spam, or denial-of-service testing
- Dependency-version alerts without a reproducible impact
- Automated scanner output without a reproducible security issue

## Safe harbor

We treat good-faith security research against code and systems you own or have permission to test as authorized when it avoids privacy violations, unauthorized data access, and service disruption. Stop and report privately if you encounter sensitive data, and give us reasonable time to investigate before public disclosure.

ViperCapture does not run a paid bug bounty program.
