# Security policy

Please report suspected vulnerabilities through GitHub's private security
advisory flow instead of a public issue. Include the affected version, a minimal
reproduction, and the impact you observed. Do not include Warcraft Logs client
secrets, access tokens, or private character data.

Security fixes target the latest published ApplicantScout Companion and its
paired `ApplicantScout-Addon` release train.

## Automated coverage

- CodeQL scans the companion's Python source on `main`, pull requests, and a
  weekly schedule with read-only repository access plus the narrow
  `security-events: write` permission required to upload findings.
- Dependabot monitors Python and pinned GitHub Actions dependencies. Repository
  dependency alerts and security updates must also remain enabled in GitHub.
- CodeQL does not support Lua. The paired addon's Lua boundary is covered by
  pinned LuaLS diagnostics, Lua 5.1 syntax checks, behavioral contract tests,
  and review; this is complementary static coverage, not a claim that Lua is
  scanned by CodeQL.
