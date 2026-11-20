"""Derive append-only synthetic observation events, never operational authorization."""


def events_for(sample, *, previous_phase, previous_door, previous_alarm, before, after):
    events = []
    def add(code, severity='INFO'):
        events.append({'sequence': sample['sequence'], 'phase': sample['phase'],
                       'phase_second': sample['phaseSecond'], 'occurred_at': sample['occurredAt'],
                       'code': code, 'severity': severity, 'provenance': 'SYNTHETIC'})
    if sample['phase'] != previous_phase:
        add('PHASE_STARTED')
    if previous_door is not None and sample['doorState'] != previous_door:
        add('DOOR_OPENED' if sample['doorState'] == 'OPEN' else 'DOOR_CLOSED')
    alarm = sample.get('criticalAlarm')
    if alarm is True and previous_alarm is not True:
        add('CRITICAL_ALARM_ACTIVATED', 'WARNING')
    elif alarm is False and previous_alarm is True:
        add('CRITICAL_ALARM_CLEARED')
    vent = sample.get('ventEvent')
    if vent in ('OPENED', 'CLOSED'):
        add('VENT_' + vent)
    for field, severity in (('warning_codes', 'WARNING'), ('failure_codes', 'FAILURE')):
        for code in sorted(set((after or {}).get(field, [])) - set((before or {}).get(field, []))):
            add(code, severity)
    if (after and after.get('ready_observed_at_second') is not None and
            (before or {}).get('ready_observed_at_second') is None):
        add('TRANSITION_READY_OBSERVED')
    if after and after.get('timeout') and not (before or {}).get('timeout'):
        add('MODE_TRANSITION_TIMEOUT', 'FAILURE')
    return events
