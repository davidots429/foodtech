"""Bind meal aggregate cleanup to the guarded deletion request."""
from .profile_deletion_recovery import ProfileDeletionRecovery


class MealAggregateDeletionRecovery(ProfileDeletionRecovery):
    """Caller authentication and writer fencing are separately required."""
