"""Host-side coordinator for provider-fork groups.

The runtime records the binding and evidence; this module only coordinates the
host facts needed to create and clean up independent branches.  It deliberately
does not wait for workers or make selection decisions.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone

from goal_plus.ports.session import DispatchProvider, ProviderDispatchReceipt, ProviderDispatchRequest


def validate_dispatch_group(requests: Sequence[ProviderDispatchRequest]) -> None:
    if not requests:
        raise ValueError("fork group requires at least one branch")
    groups = {request.dispatch_group_id for request in requests}
    if len(groups) != 1:
        raise ValueError("all fork branches must belong to one fork group")
    if any(request.checkpoint != requests[0].checkpoint for request in requests):
        raise ValueError("all fork branches must use the same checkpoint")
    indices = [request.dispatch_target_index for request in requests]
    if len(indices) != len(set(indices)):
        raise ValueError("fork branch indexes must be unique")
    # These paths are candidate identities. A remote provider must prove its
    # physical isolation with its own attachment, not a host filesystem lookup.
    workspaces = [request.workspace for request in requests]
    if len(workspaces) != len(set(workspaces)):
        raise ValueError("fork branches must use distinct workspaces")
    identities = [repr(sorted(request.worker_identity.items())) for request in requests]
    if len(identities) != len(set(identities)):
        raise ValueError("fork branches must use distinct worker identities")


class DispatchGroup:
    """Create a group with all-or-cleanup semantics for partial launch failure."""

    def __init__(
        self,
        forker: DispatchProvider,
        *,
        cleanup: Callable[[ProviderDispatchReceipt], None] | None = None,
        on_receipt: Callable[[ProviderDispatchRequest, ProviderDispatchReceipt], None] | None = None,
        on_submitted: Callable[[ProviderDispatchRequest], None] | None = None,
        on_unknown: Callable[[ProviderDispatchRequest], None] | None = None,
    ) -> None:
        self.forker = forker
        self.cleanup = cleanup
        self.on_receipt = on_receipt
        self.on_submitted = on_submitted
        self.on_unknown = on_unknown

    def start(
        self,
        requests: Sequence[ProviderDispatchRequest],
        *,
        timeout: float,
    ) -> tuple[ProviderDispatchReceipt, ...]:
        validate_dispatch_group(requests)
        receipts: list[ProviderDispatchReceipt] = []
        received_requests: list[ProviderDispatchRequest] = []
        current_request: ProviderDispatchRequest | None = None
        try:
            for request in requests:
                current_request = request
                # Persist a submission fence before crossing the provider
                # boundary.  A crash after this point is recoverable only via
                # the provider journal/receipt; it must never blindly replay a
                # possibly-created native branch.
                if self.on_submitted is not None:
                    self.on_submitted(request)
                receipt = self.forker.fork(request, timeout=timeout)
                receipts.append(receipt)
                received_requests.append(request)
                if receipt.launch_state != "started":
                    raise RuntimeError(receipt.failure or "fork branch failed")
                if (
                    receipt.runtime_provider != request.checkpoint.runtime_provider
                    or (receipt.checkpoint_id is not None and receipt.checkpoint_id != request.checkpoint.checkpoint_id)
                    or (receipt.checkpoint_kind is not None and receipt.checkpoint_kind != request.checkpoint.checkpoint_kind)
                    or receipt.parent_native_session_id != request.checkpoint.parent_native_session_id
                    or receipt.workspace != request.workspace
                    or receipt.provider_session_id == request.checkpoint.parent_native_session_id
                    or any(previous.provider_session_id == receipt.provider_session_id for previous in receipts[:-1])
                ):
                    raise RuntimeError("fork receipt has inconsistent parent, workspace or branch identity")
                if self.on_receipt is not None:
                    self.on_receipt(request, receipt)
        except BaseException as error:
            if (
                current_request is not None
                and self.on_unknown is not None
                and (
                    bool(getattr(error, "completion_unknown", False))
                    or any(token in str(error).lower() for token in ("unknown", "lost", "transport"))
                )
            ):
                self.on_unknown(current_request)
            if self.cleanup is not None:
                cleaned_ids: set[str] = set()
                for request, receipt in reversed(list(zip(received_requests, receipts, strict=True))):
                    if not receipt.provider_session_id or receipt.provider_session_id in cleaned_ids:
                        continue
                    try:
                        self.cleanup(receipt)
                        cleaned_ids.add(receipt.provider_session_id)
                        if self.on_receipt is not None:
                            self.on_receipt(request, replace(receipt, launch_state="cleaned", closed_at=datetime.now(timezone.utc).isoformat()))
                    except Exception as cleanup_error:  # noqa: BLE001 - preserve the original launch failure while recording cleanup failure
                        error.add_note(f"fork cleanup failed for {receipt.provider_session_id}: {cleanup_error}")
            raise
        return tuple(receipts)
