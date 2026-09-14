"""Trusted final-check launch declaration, shared by native host transports."""

import os

from goal_plus.domain.goal import GoalPlusFinalCheckerIdentity


FINAL_CHECK_IDENTITY_ENV = "GOAL_PLUS_FINAL_CHECK_IDENTITY"


def declared_final_checker() -> GoalPlusFinalCheckerIdentity | None:
    value = os.environ.get(FINAL_CHECK_IDENTITY_ENV)
    return GoalPlusFinalCheckerIdentity.model_validate_json(value) if value is not None else None
