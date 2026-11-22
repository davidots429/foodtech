"""Bind job cleanup steps to guarded deletion-request recovery.

Caller authentication and work dispatch remain the responsibility of a
separately authorized worker; this adapter is not an external API.
"""
from .profile_deletion_recovery import ProfileDeletionRecovery


class JobDeletionRecovery(ProfileDeletionRecovery):
    """Reuse the owner-bound recovery boundary for the job coordinator."""
