# Security Operations Severity Rubric

- **SEV-1** — Active exploitation of a known-exploited vulnerability against an
  externally exposed asset, OR confirmed compromise (data exfiltration, lateral
  movement, credential theft). Attacker has or is gaining a foothold now.
- **SEV-2** — Exploitation *attempts* against a vulnerable, reachable service with no
  confirmed compromise yet, OR a known-exploited vulnerability present on an exposed
  asset that has not been patched. Credible, imminent risk.
- **SEV-3** — Internal-only exposure, an already-patched vulnerability, or reconnaissance
  with no reachable vulnerable service. No credible path to compromise right now.

Evaluate SEV-1 first, then SEV-2, then SEV-3. Choose the first definition the incident
clearly satisfies and name the matched rule in the reason. Treat a CVE that is on the
CISA Known-Exploited-Vulnerabilities list as materially more severe than its CVSS alone.
