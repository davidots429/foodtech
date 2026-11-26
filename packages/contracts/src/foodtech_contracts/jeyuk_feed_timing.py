"""Recorded synthetic feed offsets only; no target or suitability inference."""
from datetime import datetime


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def read_feed_timing(job):
    if job.get('menu') != 'JEYUK' or job.get('provenance') != 'SYNTHETIC':
        return None
    feeds = job.get('jeyuk_feeds')
    if not isinstance(feeds, dict) or any(not isinstance(feeds.get(name), dict)
                                         for name in ('MEAT', 'VEGETABLE', 'SAUCE')):
        return {'status': 'NOT_RECORDED'}
    try:
        moments = {}
        for name in ('MEAT', 'VEGETABLE', 'SAUCE'):
            feed = feeds[name]
            if feed.get('provenance') != 'SYNTHETIC' or feed.get('ingredient') != name:
                raise ValueError
            start, end = _instant(feed.get('started_at')), _instant(feed.get('completed_at'))
            if not start or not end or end < start:
                raise ValueError
            moments[name] = (start, end)
        origin = moments['MEAT'][1]
        placed, lifted = _instant(job.get('actual_placed_at')), _instant(job.get('actual_lifted_at'))
        group = job.get('jeyuk_addition_group_id')
        if not placed or origin != placed or not lifted or lifted < origin or not isinstance(group, str) or not group or any(
                feeds[name].get('addition_group_id') != group or moments[name][0] < origin or moments[name][1] > lifted
                for name in ('VEGETABLE', 'SAUCE')):
            raise ValueError
        def micros(delta):
            return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        vegetable, sauce = (micros(moments[name][0] - origin) for name in ('VEGETABLE', 'SAUCE'))
        separation = abs(vegetable - sauce)
        simultaneous = separation <= 30_000_000
        if type(job.get('jeyuk_additions_simultaneous')) is not bool or job['jeyuk_additions_simultaneous'] != simultaneous:
            raise ValueError
        return {'status': 'RECORDED', 'basis': 'MEAT_FEED_COMPLETED', 'vegetable_start_offset_us': vegetable,
                'sauce_start_offset_us': sauce, 'start_separation_us': separation, 'simultaneous': simultaneous}
    except (TypeError, ValueError, OverflowError):
        return {'status': 'INCONSISTENT'}
