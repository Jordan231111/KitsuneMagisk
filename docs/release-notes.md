# KitsuneMagisk development build

This is a test build from the resumed KitsuneMagisk project, not a production release.

## Current test-build changes

- Harden root-backend error handling, package identity checks, and concurrent `su` use.
- Preserve legacy Kitsune HideList selections when opening an older development database.
- Verify app updates with HTTPS metadata and SHA-256 before installation or dynamic loading.
- Improve hidden-manager installation, download, job, and activity lifecycle handling.
- Let a canceled boot-image selection be retried without leaving the install method stuck.
- Keep Direct-System/System Mode in debug builds while its persistent rollback lifecycle is
  qualified on named writable targets.

## Before installing

Use a disposable emulator or make a verified device snapshot/backup. A normal rooted backend does
not prove that the Android system partition is writable. System Mode must reject read-only EROFS or
verified layouts without changing them.

Technical evidence and open blockers are maintained in the repository's support-status and
development-roadmap documents, not in this screen.
