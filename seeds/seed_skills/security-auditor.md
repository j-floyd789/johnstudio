---
name: security-auditor
description: "Security review patterns: secret handling, OWASP top-10, dangerous commands"
category: security
tags: [security, owasp, secrets, audit, dependencies]
languages: []
frameworks: []
agent_roles: [security_reviewer]
file_patterns: ["**/*"]
priority: high
---

# Security Auditor

## When to activate
- ANY diff that touches authentication, authorization, file I/O, subprocess, network.
- Before merging changes that add dependencies.

## Checklist
- Scan for hardcoded secrets, tokens, API keys.
- Check for SQL injection in raw queries (`f"SELECT * FROM x WHERE id = {id}"`).
- Validate URL schemes (`http`/`https` only) for outbound requests.
- Check file paths for traversal (`../../`).
- Confirm webhook signatures verified.

## Must / never
- **must** flag any change under protected paths: `.env`, `*.pem`, `*.key`, `~/.ssh/**`, `~/.aws/**`.
- **must** flag dangerous commands: `rm -rf`, `sudo`, `curl | bash`, `chmod -R 777`, `git push --force`.
- **never** approve a diff that prints secrets to logs.
- **never** approve dependency installs without a maintainer/version check.

## Anti-patterns
- Adding `// nosec` comments instead of fixing the issue.
- Catch-all `except Exception` that swallows security errors.
- Trusting user input as filesystem paths.
