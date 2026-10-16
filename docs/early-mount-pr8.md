# PR8 early-mount design and qualification fixtures

Status: historical design input. PR14 implements the [versioned module contract](module-contract.md)
with [scoped runtime evidence](module-pr14.json). This proposed matrix remains a qualification
input; its generated cases are not automatically passed by the implementation.
The historical requirements remain MOD-001 through MOD-004, MOD-009 through MOD-012,
MOD-017 through MOD-020, and their Hide/SuList interactions in the port ledger.

An early mount is accepted only when the intended bytes are visible in the consumer's mount
namespace **before that consumer first opens the target**. A post-fs-data marker, a successful
module installation, or a file visible only to a root shell is insufficient evidence.

## Route boundaries

| Route | Required integration point | Evidence required before claiming support |
|---|---|---|
| Patched boot / init_boot | MagiskInit before handing control to the Android init stage that consumes the target | Exact image/slot, pre-init storage availability, mount publication and first-consumer trace |
| Qualified vendor_boot / recovery route | The corresponding tested MagiskInit handoff; no assumption that every vendor ramdisk is equivalent | Ramdisk selection, normal-versus-recovery boot, encryption state and stock recovery |
| Managed System Mode | A proved init import and trigger preceding the particular consumer | Import/trigger trace and executable policy domain; reject targets consumed before this hook |

For example, a VINTF manifest overlay must precede its actual first reader on the tested image.
The reader and timing must be measured on that image. This document does not assign one universal
Android boot event to every vendor's VINTF, property, fstab, or init-RC consumption.

## Proposed v2 plan

PR14 should stage a versioned plan containing module ID/version, target partition and path,
source digest and metadata, installation route, earliest consumer, conflict disposition, and
recovery ownership. The plan and payload must be available before the chosen hook; encrypted
`/data` must not be assumed readable. Reuse the maintained upstream mount/policy primitives.

EROFS and verified read-only partitions require a reversible overlay in the intended namespace.
Do not make the base partition writable to satisfy a module. Preserve ordinary files, symbolic
links, whiteout character devices, and `.replace` directories as distinct operations. Targets
escaping the declared partition or plan are rejected before publication.

Global and per-module trees need a recorded, deterministic conflict result. Before selecting the
legacy compatibility rule, run the same conflicting modules on the historical implementation and
record the visible winner. Do not silently impose a different order. A v2 priority mechanism may
be introduced only with explicit versioning and migration guidance.

Publication must be recoverable after every interruption. A failed module must leave either its
previous complete plan or no active plan, and base Android must remain bootable. Safe-mode and
disable/remove decisions must take effect before the same first consumer, including while `/data`
is encrypted. PR14 must define the protected pre-init recovery marker and input path for each route.

## Fixture set

The machine-readable cases in [early-mount-pr8.json](early-mount-pr8.json) are synthetic test
specifications, not device results. Run every case on each claimed route. Capture the boot ID,
artifact and module digests, mount namespace/inode, mount IDs, original and visible file digests,
publication event, first-consumer event, SELinux state/AVCs, and restore result.

Build the concrete fixture payloads with:

```sh
python tools/early_mount_fixtures.py --output out/early-mount-fixtures
```

The new output directory contains two conflicting module ZIPs, base/early tree TARs, a dedicated
read-only consumer and a 75-case plan (15 cases across five routes), all initially `not_run`.
Base, ordinary and early payloads have different bytes, so a late ordinary mount cannot impersonate
an early result. The trees include a symlink, a true 0:0 character-device whiteout, a `.replace`
directory and unrelated content that must survive. The TARs remain inert on the host.

Stage them only through the qualified disposable-image adapter with a verified external restore.
That adapter must select and record the route's actual init hook and policy domain; the generator
does not inject an RC file, mount an image or install a module. Capture `consumer.sh first-consumer`
at that measured point, plus `post-fs-data-control`, `after-disable` and `after-restore` samples.
The dedicated consumer is a timing fixture, not evidence about an uninstrumented Android service.
For global/per-module conflicts, stage the same early tree globally and record the historical winner
in both installation orders before choosing the v2 rule. Fault, EROFS, encrypted-data, policy and
slot cases require the named real image condition; generating this plan does not pass them.

Keep paired current/next traces for successful cases and failures. PR8 owns the comparison and
timing design; PR14 owns implementation and module migration; PR15 owns target breadth. PR16
requires exact-candidate evidence and rejects unresolved historical rows.
