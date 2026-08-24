"""Capability-oriented System Mode adapter contracts.

Tier A runs the shared transaction inside a qualified guest. Tier B adapters
must work on a stopped, copied image and are deliberately host-only; vendor
discovery and image conversion belong in concrete implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ADAPTER_SCHEMA_VERSION = 1


class AdapterError(ValueError):
    """An adapter plan violates the safety contract."""


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    tier: str
    execution: str
    capabilities: tuple[str, ...]
    requires_stopped_target: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "adapter_id": self.adapter_id,
            "tier": self.tier,
            "execution": self.execution,
            "capabilities": list(self.capabilities),
            "requires_stopped_target": self.requires_stopped_target,
        }


@dataclass(frozen=True)
class HostImagePlan:
    source_image: Path
    working_copy: Path
    external_backup: Path
    restore_command: Sequence[str]
    target_stopped: bool

    def validate(self) -> None:
        paths = tuple(path.expanduser().resolve() for path in (
            self.source_image,
            self.working_copy,
            self.external_backup,
        ))
        if len(set(paths)) != len(paths):
            raise AdapterError("source, working copy, and backup must be distinct paths")
        if not self.target_stopped:
            raise AdapterError("host-image mutation requires a stopped target")
        if not self.restore_command:
            raise AdapterError("host-image mutation requires an explicit restore command")
        if paths[1] == paths[0] or paths[2] == paths[0]:
            raise AdapterError("an adapter may never patch the live vendor image in place")


class SystemModeAdapter(ABC):
    descriptor: AdapterDescriptor

    @abstractmethod
    def supports(self, report: Mapping[str, Any]) -> bool:
        """Return whether the report satisfies this adapter's capabilities."""


class GenericInGuestAdapter(SystemModeAdapter):
    descriptor = AdapterDescriptor(
        adapter_id="generic-in-guest",
        tier="A",
        execution="in_guest",
        capabilities=(
            "bootstrap_root",
            "persistent_writable_system",
            "init_import",
            "selinux_policy",
            "external_recovery",
        ),
        requires_stopped_target=False,
    )

    def supports(self, report: Mapping[str, Any]) -> bool:
        return report.get("assessment", {}).get("verdict") == "supported"


class GenericHostImageAdapter(SystemModeAdapter, ABC):
    descriptor = AdapterDescriptor(
        adapter_id="generic-host-image",
        tier="B",
        execution="host_image",
        capabilities=(
            "stopped_target",
            "copied_image",
            "format_validation",
            "external_recovery",
        ),
        requires_stopped_target=True,
    )

    def supports(self, report: Mapping[str, Any]) -> bool:
        reasons = set(report.get("assessment", {}).get("reason_codes", ()))
        return "NO_BOOTSTRAP_ROOT" in reasons or "READ_ONLY_FS" in reasons

    @abstractmethod
    def discover(self, target: str) -> HostImagePlan:
        """Resolve a stopped source image, copy, backup, and restore tuple."""

    @abstractmethod
    def validate_format(self, plan: HostImagePlan) -> None:
        """Fail closed when the copied vendor image format is unsupported."""

    @abstractmethod
    def stage(self, plan: HostImagePlan, payload: Path) -> None:
        """Mutate only ``plan.working_copy`` with a manifest-owned transaction."""

    @abstractmethod
    def verify(self, plan: HostImagePlan) -> None:
        """Verify the stopped copy before a vendor-specific atomic replacement."""


def built_in_descriptors() -> tuple[AdapterDescriptor, ...]:
    return (GenericInGuestAdapter.descriptor, GenericHostImageAdapter.descriptor)
