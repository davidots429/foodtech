"""Explicit mapping from logical programme racks to newly installed rack IDs."""
from decimal import Decimal
import re


def _index(value, maximum):
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value != int(value) or not 1 <= value <= maximum:
        raise ValueError('bounded layout index required')
    return int(value)


def installed_rack_count(size, positions, products, rack_order):
    if size not in {'MEDIUM_FIXED_RACK_10','LARGE_ROLL_IN_RACK_20'}:
        raise ValueError('supported oven profile required')
    maximum = 1 if size == 'MEDIUM_FIXED_RACK_10' else 2
    if type(rack_order) is not list or not 1 <= len(rack_order) <= maximum or \
            any(type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}', value) for value in rack_order) or \
            len(set(rack_order)) != len(rack_order):
        raise ValueError('explicit unique installed rack order required')
    if type(positions) is not list or not 1 <= len(positions) <= maximum*10 or not products:
        raise ValueError('planned pans and fresh products required')
    planned = {(_index(p['rack'],maximum), _index(p['shelf'],10)) for p in positions}
    if len(planned) != len(positions):
        raise ValueError('duplicate planned pan position')
    indices = {rack:i for i,rack in enumerate(rack_order,1)}
    pans = {p['pan_id']:(p['rack_id'],_index(p['shelf'],10)) for p in products}
    shelves = {(p['rack_id'],_index(p['shelf'],10)):p['pan_id'] for p in products}
    if any(p['rack_id'] not in indices or pans[p['pan_id']] != (p['rack_id'],p['shelf']) or
           shelves[p['rack_id'],p['shelf']] != p['pan_id'] for p in products):
        raise ValueError('fresh products have ambiguous rack or pan locations')
    actual = {(indices[rack],shelf) for rack,shelf in pans.values()}
    if actual != planned:
        raise ValueError('fresh pan locations differ from confirmed candidate positions')
    # An installed empty rack is still weighed. Logical rack 2 must never be
    # silently renumbered as rack 1 because it is the only rack holding products.
    return len(rack_order)
