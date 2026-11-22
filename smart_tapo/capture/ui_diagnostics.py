"""Closed, credential-free schema for UI verification observations."""
import math

SAFE_REASONS = frozenset({
    'unsupported_package', 'invalid_hierarchy', 'sensitive_page',
    'observed_playback_layout', 'incomplete_playback_layout',
    'playback_or_conflicting_controls', 'unrecognized_foreground',
    'camera_not_confirmed', 'ambiguous_camera_title', 'incomplete_live_layout',
    'live_refresh_required', 'foreground_changed_or_unknown', 'navigation_in_progress',
    'live_inactivity_confirmation', 'selected_camera_authorization_missing',
})
UI_CHECK_STATES = frozenset({'valid', 'pending_expired', 'rejected', 'failed'})
UI_CHECK_STAGES = frozenset({'idle', 'lock', 'foreground_before', 'ui_dump', 'ui_read',
                            'ui_cleanup', 'foreground_after', 'classify', 'unknown'})
UI_CHECK_REASONS = SAFE_REASONS | frozenset({'proof_expired', 'adb_timeout',
    'adb_unavailable', 'adb_nonzero', 'ui_lock_busy', 'ui_lock_unavailable',
    'ui_dump_failed', 'ui_hierarchy_invalid', 'verification_exception',
    'legacy_rejected', 'none', 'unrecognized_reason'})
TIME_FIELDS = ('check_seconds', 'proof_age_seconds', 'lock_seconds',
               'foreground_seconds', 'dump_seconds', 'read_seconds',
               'cleanup_seconds', 'parse_seconds')


def valid_ui_check(detail):
    if type(detail) is not dict or set(detail) != {'state', 'reason', 'stage', *TIME_FIELDS}:
        return False
    if (type(detail['state']) is not str or detail['state'] not in UI_CHECK_STATES
            or type(detail['reason']) is not str or detail['reason'] not in UI_CHECK_REASONS
            or type(detail['stage']) is not str or detail['stage'] not in UI_CHECK_STAGES):
        return False
    return all(detail[key] is None or (type(detail[key]) is float
               and math.isfinite(detail[key]) and 0 <= detail[key] <= 86400)
               for key in TIME_FIELDS)


def observation():
    return dict(stage='unknown', **{key: None for key in TIME_FIELDS})
