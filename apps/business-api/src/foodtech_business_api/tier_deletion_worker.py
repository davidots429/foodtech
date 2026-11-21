"""Run one bounded, restartable tier-deletion page inside a trusted worker.

The caller must authenticate and authorize the deletion job. Progress storage
and the tier store must enforce the same request lock. This worker does not
verify indexes, backups, other domains, or mark the deletion complete.
"""


import json


class TierIndexCapture:
    """Persist encrypted index candidates before any tier record is deleted."""
    def __init__(self, *, tier_store, progress_store, cipher):
        self.tier_store, self.progress_store, self.cipher = tier_store, progress_store, cipher

    @staticmethod
    def _decode(raw):
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"version", "cursor", "keys", "complete"} or \
                    data["version"] != 1 or type(data["complete"]) is not bool or \
                    not isinstance(data["keys"], list) or any(not isinstance(k, str) or not k or len(k) > 2048 for k in data["keys"]) or \
                    len(set(data["keys"])) != len(data["keys"]) or \
                    (data["cursor"] is not None and (not isinstance(data["cursor"], str) or not data["cursor"].startswith("APPLICATION#"))) or \
                    data["complete"] != (data["cursor"] is None):
                raise ValueError
            return data
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid tier index capture") from None

    def capture_page(self, *, subject, request_id, limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid tier capture limit")
        scope = {"subject": subject, "request_id": request_id, "domain": "TIER_INDEX"}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is None:
            version, cursor, keys = 0, None, []
        else:
            data = self._decode(previous["checkpoint"])
            if data["complete"]:
                return {"complete": True, "version": previous["version"], "captured_count": len(data["keys"])}
            version, cursor, keys = previous["version"], data["cursor"], data["keys"]
        page = self.tier_store.deletion_index_candidates(subject, request_id, limit, cursor)
        combined = keys + page["queue_sorts"]
        data = {"version": 1, "cursor": page["next_cursor"], "keys": combined,
                "complete": page["next_cursor"] is None}
        raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        if len(raw) > 16384:
            raise ValueError("tier index capture exceeds checkpoint capacity")
        self._decode(raw)
        saved = self.progress_store.save_checkpoint(cipher=self.cipher, **scope, expected_version=version,
            state="VERIFYING" if data["complete"] else "IN_PROGRESS", checkpoint=raw)
        return {"complete": data["complete"], "version": saved, "captured_count": len(combined)}

    def ensure_ready(self, *, subject, request_id):
        entry = self.progress_store.load_checkpoint(cipher=self.cipher, subject=subject, request_id=request_id,
                                                    domain="TIER_INDEX")
        if not entry or entry.get("state") != "VERIFYING" or not self._decode(entry["checkpoint"])["complete"]:
            raise ValueError("tier index capture is not complete")


class TierDeletionWorker:
    def __init__(self, *, tier_store, progress_store, cipher, index_capture):
        self.index_capture = index_capture
        self.tier_store = tier_store
        self.progress_store = progress_store
        self.cipher = cipher

    def run_page(self, *, subject: str, request_id: str, limit: int) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid tier deletion page limit')
        self.index_capture.ensure_ready(subject=subject, request_id=request_id)
        scope = {'subject': subject, 'request_id': request_id, 'domain': 'TIER'}
        previous = self.progress_store.load_checkpoint(cipher=self.cipher, **scope)
        if previous is None:
            version = 0
        else:
            if not isinstance(previous, dict) or previous.get('checkpoint') != b'tier-first-page-v1' or \
                    type(previous.get('version')) is not int or not 1 <= previous['version'] < 2**63 - 1 or \
                    previous.get('state') not in {'IN_PROGRESS', 'VERIFYING', 'FAILED'}:
                raise ValueError('invalid tier deletion progress')
            version = previous['version']
        # Always restart at the first remaining key. Every deletion rechecks
        # the lock; if saving progress fails, the page can safely be retried.
        count = self.tier_store.delete_inventory_page(subject, request_id, limit)
        if type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid tier deletion page result')
        state = 'VERIFYING' if count == 0 else 'IN_PROGRESS'
        version = self.progress_store.save_checkpoint(cipher=self.cipher, **scope,
            expected_version=version, state=state, checkpoint=b'tier-first-page-v1')
        return {'deleted_count': count, 'version': version, 'state': state}
