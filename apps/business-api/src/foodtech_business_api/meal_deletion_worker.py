"""Run one locked cleanup step for a verified captured meal target.

This internal worker does not authenticate callers or establish completeness.
"""
from .meal_partition_deletion import delete_one_meal_record


class MealDeletionWorker:
    def __init__(self, *, inventory, source_table, progress_store, cipher):
        if inventory.source_table is not source_table or inventory.progress_store is not progress_store or \
                inventory.cipher is not cipher:
            raise ValueError('meal deletion dependencies do not match')
        self.inventory, self.source_table = inventory, source_table
        self.progress_store, self.cipher = progress_store, cipher

    def delete_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid meal deletion index')
        target = next((target for n, target in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if n == index), None)
        if target is None:
            raise ValueError('meal deletion target unavailable')
        result = delete_one_meal_record(source_table=self.source_table, progress_store=self.progress_store,
            cipher=self.cipher, subject=subject, request_id=request_id, meal_id=target.meal_id)
        return {'index': index, 'state': result['state'], 'removed': result['removed']}
