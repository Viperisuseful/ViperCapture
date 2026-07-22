# Security Policy

## Supported Versions

ViperCapture currently follows continuous development rather than maintaining multiple versioned release branches.

| Version                                   | Supported          |
| ----------------------------------------- | ------------------ |
| Latest code on `master`                   | :white_check_mark: |
| Latest hosted deployment                  | :white_check_mark: |
| Older commits or archived releases        | :x:                |
| Third-party forks or modified deployments | :x:                |

The `/v1/` prefix used by the API represents the API contract version and does not indicate a separately maintained software release branch.

## Reporting a Vulnerability

Please report suspected security vulnerabilities privately. **Do not open a public GitHub issue, discussion, or pull request containing vulnerability details.**

Use GitHub's private vulnerability reporting feature:

1. Open the **Security** tab of this repository.
2. Select **Advisories**.
3. Select **Report a vulnerability**.

If private vulnerability reporting is unavailable, contact the repository owner through their GitHub profile and request a private communication channel. Do not include technical vulnerability details in a public message.

### Information to Include

Provide as much of the following information as possible:

* A clear description of the vulnerability
* Whether it affects the hosted service, self-hosted version, or both
* The affected endpoint, component, or configuration
* Steps required to reproduce the issue
* A minimal proof of concept, when safe to provide
* The potential security impact
* Any suggested mitigation or fix
* Your preferred name or handle for acknowledgment

Reports involving SSRF, access to private networks or cloud metadata, unauthorized header disclosure, arbitrary file access, command execution, authentication bypass, or unauthorized account access are especially important.

Do not access, modify, download, or retain data belonging to other users while testing.

## Response Process

After receiving a report, the maintainer will aim to:

* Acknowledge the report within **3 business days**
* Provide an initial assessment within **7 business days**
* Send progress updates at least every **14 days** while the report remains unresolved
* Confirm whether the report has been accepted, declined, or requires more information
* Coordinate disclosure after a fix or mitigation is available

Response times may vary depending on the complexity and severity of the vulnerability.

If a report is accepted, the maintainer will investigate the issue, prepare an appropriate fix or mitigation, and coordinate a responsible disclosure timeline with the reporter.

If a report is declined, the maintainer will explain the reason when reasonably possible.

## Scope

This policy covers security issues affecting:

* The ViperCapture source code in this repository
* The webpage rendering API and browser-based interface
* The official hosted ViperCapture service
* Security boundaries related to browser automation, URL validation, request headers, rendering, and network access

The following are generally outside the scope of this policy:

* Vulnerabilities in third-party websites captured through ViperCapture
* Issues found only in unofficial forks or heavily modified deployments
* Missing security controls already documented as the responsibility of self-hosting operators
* Social engineering, spam, or denial-of-service testing
* Automated reports without a reproducible security impact
* Reports describing only outdated dependencies without demonstrating exploitability
* Publicly disclosed vulnerabilities submitted before allowing reasonable time for investigation

## Safe Harbor

Good-faith security research performed in accordance with this policy will be treated as authorized.

Researchers must:

* Avoid privacy violations and disruption of the service
* Use only accounts and data they own or have permission to access
* Stop testing and report the issue if sensitive information is encountered
* Provide reasonable time for investigation and remediation before disclosure

ViperCapture does not currently operate a paid bug bounty program. Recognition may be provided to reporters who request it and assist with responsible disclosure.
