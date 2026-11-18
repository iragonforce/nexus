# Nexus — Agent Instructions

## Scope

This repository contains curated, public, host-specific
configuration for the Nexus Debian server.

The private `~/ops` repository remains authoritative for
existing managed deployments and shared infrastructure.

## Source Ownership

- Consult `~/ops/AGENTS.md` and `docs/START-HERE.md`.
- Check `inventory/deployments.json` before adopting files.
- Do not create competing authoritative configuration sources.
- Preserve original file paths, permissions, and provenance.
- Repository snapshots do not automatically represent live state.

## Security

- Only adopt explicitly reviewed, non-secret configuration.
- Never commit credentials, tokens, private keys, databases,
  application state, or raw backups.
- Treat public Git history as permanent.
- Review staged changes and scan for secrets before publication.
- Do not expand the file allowlist without reviewing each addition.

## Operations

- Inspect before modifying.
- Preserve existing working services and configurations.
- Do not deploy or restart services without explicit authorization.
- Validate changes and document exact rollback procedures.
- Do not create redundant automation or deployment mechanisms.
