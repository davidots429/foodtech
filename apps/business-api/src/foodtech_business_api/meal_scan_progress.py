"""Protected meal scan progress, for an authorized maintenance worker only."""
from .legacy_meal_scan import reconcile_legacy_meal_page
from .profile_checkpoint_cipher import ProfileCheckpointCipher
from .profile_scan_progress import ProfileScanProgressStore, _job


class MealCheckpointCipher(ProfileCheckpointCipher):
    def _context(self, job_id, phase, version, source_table):
        return {**super()._context(job_id, phase, version, source_table),
                'purpose': 'MEAL_MIGRATION_CHECKPOINT'}


def _meal_job(job_id):
    key = _job(job_id)
    return {**key, 'pk': key['pk'].replace('PROFILE_SCAN#', 'MEAL_SCAN#', 1)}


class MealScanProgressStore(ProfileScanProgressStore):
    _key = staticmethod(_meal_job)

    def __init__(self, table, cipher):
        if not isinstance(cipher, MealCheckpointCipher):
            raise ValueError('meal checkpoint cipher required')
        super().__init__(table, cipher)

    def _reconcile(self, table, store, **kwargs):
        return reconcile_legacy_meal_page(table, store, **kwargs)
