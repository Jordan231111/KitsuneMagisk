# Kitsune module template

Copy `module.prop` and `customize.sh` into a new module directory, replace the
metadata, add your payload, and ZIP the directory contents. Keep the capability
guards when adding author customization. The ordinary Magisk `system/`, script,
`REMOVE`, `.replace` and `action.sh` interfaces remain available.

Read [the module contract](../../docs/module-contract.md) before using `root/`,
`early-mount/`, `initrc.d` or `boot-completed.sh`. Early mounting additionally
requires a successful runtime route check. Test installation, an interrupted
update, disable/remove, boot order and recovery on a disposable target.

This replaces the version-suffix and numeric-version guards from the historical
Kitsune module-template ZIP. Its immutable original bytes and digest remain in
[the historical record](../../docs/port-history/module-template.json). Do not
silently download or execute that old template as a current SDK. In particular,
its arbitrary init-RC example needs migration to a namespaced, bounded service.

Former Lite users should start with all modules disabled and explicitly enable
one verified module at a time. There is no separate Lite module runtime or an
automatic activation of previously unsupported modules.
