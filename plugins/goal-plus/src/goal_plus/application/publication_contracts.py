"""Derive the exact business intent from a prepared publication."""

from goal_plus.domain.promotion import PromotionRecord
from goal_plus.ports.publication import PublicationApplyIntent


def publication_apply_intent(
    promotion: PromotionRecord,
    publication_revision: int,
) -> PublicationApplyIntent:
    publication = promotion.publication_artifact_ref
    if publication is None:
        raise RuntimeError("ThinkThread promotion has no publication snapshot")
    dependencies = promotion.provider_handle.get("dependencies") or []
    if not isinstance(dependencies, list) or any(
        not isinstance(item, dict) for item in dependencies
    ):
        raise TypeError("ThinkThread promotion dependencies are invalid")
    return PublicationApplyIntent(
        run_id=promotion.run_id,
        publication_revision=publication_revision,
        base_artifact_ref=promotion.base_artifact_ref,
        target_artifact_ref=publication,
        dependencies=tuple(dependencies),
    )
