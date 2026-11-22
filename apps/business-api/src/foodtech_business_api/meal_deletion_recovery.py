"""Bind meal cleanup to guarded deletion-request recovery.

The caller must be a separately authenticated and authorized worker.
"""
from .profile_deletion_recovery import ProfileDeletionRecovery


class MealDeletionRecovery(ProfileDeletionRecovery):
    """Use the owner-bound recovery boundary for the meal coordinator."""
