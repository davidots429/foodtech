"""Project-defined synthetic MQTT CSV columns; preserve source bytes separately."""
import csv
import io
import json
from decimal import Decimal, InvalidOperation
from importlib.resources import files

COLUMNS = {v: json.loads(files('foodtech_contracts').joinpath(
    f'mockprofile-302-{v}-v1.columns.json').read_text())['columns'] for v in 'abc'}
PREFIXES = tuple(','.join(COLUMNS[v][:2]) + ',' for v in 'abc')
EQUIPMENT_COLUMNS = {(profile, vendor): json.loads(files('foodtech_contracts').joinpath(
    f'mockprofile-{profile}-{vendor}-v1.columns.json').read_text())['columns']
    for profile in ('312', '322') for vendor in 'abc'}


def is_csv(raw: str | bytes) -> bool:
    return any(raw.startswith(p.encode() if isinstance(raw, bytes) else p) for p in PREFIXES)


def parse_record(raw: str | bytes) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='strict')
    if len(raw.encode('utf-8')) > 2_000_000:
        raise ValueError('CSV record too large')
    try:
        rows = list(csv.reader(io.StringIO(raw, newline=''), strict=True))
    except csv.Error as exc:
        raise ValueError('invalid CSV') from exc
    if len(rows) != 2 or len(rows[0]) != len(rows[1]) or len(set(rows[0])) != len(rows[0]):
        raise ValueError('CSV requires one unambiguous header and row')
    if any(value.startswith(('=', '+', '-', '@', '\t', '\r')) for row in rows for value in row):
        raise ValueError('CSV formula-risk cell')
    vendor = next((v for v, cols in COLUMNS.items() if rows[0][:len(cols)] == cols), None)
    if vendor is None:
        raise ValueError('CSV column schema mismatch')
    row = dict(zip(*rows))
    instance = row.get({'a': 'deviceId', 'b': 'device_id', 'c': 'DeviceId'}[vendor], '')
    equipment_profile = next((profile for profile in ('312', '322')
                              if instance == f'mockprofile-{profile}-{vendor}-001'), None)
    if equipment_profile and rows[0][:len(EQUIPMENT_COLUMNS[(equipment_profile, vendor)])] != EQUIPMENT_COLUMNS[(equipment_profile, vendor)]:
        raise ValueError('equipment CSV column schema mismatch')
    def number(key):
        try:
            value = Decimal(row[key])
            if not value.is_finite(): raise ValueError('non-finite value')
            return value
        except InvalidOperation as exc:
            raise ValueError('invalid numeric cell') from exc
    def integer(key):
        value = row[key]
        if not value.isascii() or not value.isdigit(): raise ValueError('invalid integer cell')
        return int(value)
    def optional_number(key):
        return None if row[key] == '' else number(key)
    if vendor == 'a':
        result = {key: row[key] for key in COLUMNS[vendor][:8]}
        result['sequenceNumber'] = integer('sequenceNumber')
        result['measurements'] = {'temperatureCelsius': number('measurements.temperatureCelsius'),
                                  'speedRpm': integer('measurements.speedRpm'),
                                  'elapsedSeconds': integer('measurements.elapsedSeconds')}
        result['errorReason'] = row['errorReason'] or None
        result['extensions'] = {'firmwareVersion': row['extensions.firmwareVersion']}
    elif vendor == 'b':
        result = {key: row[key] for key in COLUMNS[vendor]}
        for key in ('sequence_number', 'event_time_ms', 'device_status', 'elapsed_ms'):
            result[key] = integer(key)
        for key in ('temperature_f', 'output_percent'): result[key] = number(key)
    else:
        result = {key: row[key] for key in COLUMNS[vendor][:8]}
        result['SeqNo'] = integer('SeqNo')
        result['Readings'] = {'TempC': number('Readings_TempC'), 'SpeedLvl': integer('Readings_SpeedLvl'),
                              'ElapsedMin': number('Readings_ElapsedMin')}
        result.update(Alarm=row['Alarm'], Err=row['Err'], Extension={'Firmware': row['Extension_Firmware']})
    if equipment_profile:
        if vendor == 'a':
            result['extensions']['equipmentType'] = row['extensions.equipmentType']
            if equipment_profile == '322':
                result['extensions'].update(climateSetpointPercent=number('extensions.climateSetpointPercent'),
                    humiditySource=row['extensions.humiditySource'], humidityPercent=optional_number('extensions.humidityPercent'),
                    fanLevel=integer('extensions.fanLevel'))
        elif vendor == 'b':
            result['ext_equipment_type'] = row['ext_equipment_type']
            if equipment_profile == '322':
                result.update(ext_climate_setpoint_percent=number('ext_climate_setpoint_percent'),
                    ext_humidity_source=row['ext_humidity_source'], ext_humidity_percent=optional_number('ext_humidity_percent'),
                    ext_fan_level=integer('ext_fan_level'))
        else:
            result['Extension']['EquipmentType'] = row['Extension_EquipmentType']
            if equipment_profile == '322':
                result['Extension'].update(ClimateSetpointPct=number('Extension_ClimateSetpointPct'),
                    HumiditySource=row['Extension_HumiditySource'], HumidityPct=optional_number('Extension_HumidityPct'),
                    FanLevel=integer('Extension_FanLevel'))
    # Additional columns stay in the original CSV and are not promoted to standard fields.
    return result
