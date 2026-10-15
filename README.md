# KitsuneMagisk update feeds

Beta and canary contain signed experimental metadata. APKs are served from immutable project releases. Stable promotion remains gated by roadmap PR16.

The [update contract](https://github.com/Jordan231111/KitsuneMagisk/blob/a95795049614bd09019af69ddd9c968586ae8f6f/docs/update-service.md) defines signer pins, rollback protection, expiry and publication checks. [PR10](https://github.com/Jordan231111/KitsuneMagisk/pull/33) records acceptance evidence.

For a metadata-only refresh, check out the current release's source tag and use its exact published APKs without rebuilding. Increase the channel sequence, preserve the other feeds and commit history, and verify the public downloads after publication.
