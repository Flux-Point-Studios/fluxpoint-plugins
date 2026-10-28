"""Pinned repair operating limits and data-derived production alarm counts."""
import datetime
import json
import math


def bounds_issue(bounds):
    if bounds is None:
        return None
    if not isinstance(bounds, dict) or set(bounds) - {'deadlineMs', 'maxPerDay', 'providerPools'}:
        return 'operationalBounds allows deadlineMs, maxPerDay and providerPools only'
    for key in ('deadlineMs', 'maxPerDay'):
        if key in bounds and (type(bounds[key]) not in (int, float) or not math.isfinite(bounds[key])
                or bounds[key] < 0 or key == 'deadlineMs' and bounds[key] == 0):
            return 'operationalBounds.' + key + ' must be a finite positive deadline or nonnegative rate'
    pools = bounds.get('providerPools')
    if pools is not None and (not isinstance(pools, list) or any(not isinstance(p, str) or not p.strip() for p in pools)
            or len(set(pools)) != len(pools)):
        return 'operationalBounds.providerPools must be a unique array of pool names'
    return None


def combine_bounds(values):
    values = [v for v in values if isinstance(v, dict)]
    result = {}
    for key in ('deadlineMs', 'maxPerDay'):
        found = [v[key] for v in values if key in v]
        if found:
            result[key] = min(found)
    if any('providerPools' in v for v in values):
        result['providerPools'] = sorted({p for v in values for p in v.get('providerPools', [])})
    return result or None


def series_metrics(data):
    """RepairAlarmSeriesV1: explicit observation window and adjudicated events.

    Event falsePositive labels are the operator's recorded observations. The
    helper computes the duration/count; command stdout supplies neither one.
    """
    def unique(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate production-series key: ' + key)
            out[key] = value
        return out
    doc = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
    if not isinstance(doc, dict) or (type(doc.get('version')) is not int or doc.get('version') != 1) or not isinstance(doc.get('events'), list):
        raise ValueError('production series must be RepairAlarmSeriesV1 with version, startedAt, endedAt and events')
    def instant(value):
        if not isinstance(value, str):
            raise ValueError('production-series timestamps must be timezone-aware ISO-8601 instants')
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('production-series timestamps need an explicit timezone')
        return parsed
    start, end = instant(doc.get('startedAt')), instant(doc.get('endedAt'))
    duration = (end - start).total_seconds()
    if duration <= 0:
        raise ValueError('production-series endedAt must follow startedAt')
    seen, false = set(), 0
    for event in doc['events']:
        if (not isinstance(event, dict) or not isinstance(event.get('id'), str) or not event['id'].strip()
                or event['id'] in seen or type(event.get('falsePositive')) is not bool):
            raise ValueError('production-series events need unique ids and boolean falsePositive labels')
        seen.add(event['id'])
        if not start <= instant(event.get('at')) <= end:
            raise ValueError('production-series event falls outside the observation window')
        false += event['falsePositive']
    return dict(durationSeconds=duration, falsePositives=false)
