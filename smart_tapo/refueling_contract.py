"""Strict outbound v1 metadata: no arbitrary attributes or local file paths."""
from datetime import datetime, timezone
from fractions import Fraction
import json
import re

MAX_JSON=64*1024
MAX_IMAGE=2*1024*1024
ERROR_CODES=frozenset({
    'timeout','network_error','transport_error','http_error','unauthorized','forbidden','rate_limited',
    'server_error','invalid_response','media_unavailable','configuration_error','image_upload_failed',
    'event_upload_failed','delivery_error','invalid_idempotency_key','redirect_disallowed',
    'receiver_unavailable','receiver_auth_rejected','receiver_response_too_large','invalid_receiver_response',
    'receiver_images_missing','receiver_rejected','invalid_receiver_ack','transport_unconfirmed',
    'invalid_or_missing_media','invalid_payload','destination_mismatch','receiver_media_contract_mismatch',
    'expired_before_delivery_confirmation','receiver_firewall_blocked'})


def _keys(value,keys):
    if not isinstance(value,dict) or set(value)!=set(keys.split()):raise ValueError('invalid_payload')


def _match(value,pattern):
    if not isinstance(value,str) or not re.fullmatch(pattern,value):raise ValueError('invalid_payload')


def _date(value):
    if not isinstance(value,str) or len(value)>40:raise ValueError('invalid_payload')
    date=datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None or date.timestamp()<0:raise ValueError('invalid_payload')
    return date.timestamp()


def validate_event(payload,camera_id=None,event_id=None):
    if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_JSON:raise ValueError('invalid_payload')
    value=json.loads(payload.decode('utf-8'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid_payload')))
    _keys(value,'schema_version event_id event_type occurred_at session')
    if type(value['schema_version']) is not int or value['schema_version']!=1:raise ValueError('invalid_payload')
    if value['event_type'] not in ('refueling.started','refueling.updated','refueling.attention_required','refueling.completed'):
        raise ValueError('invalid_payload')
    _date(value['occurred_at'])
    s=value['session']
    _keys(s,'id camera_id revision state started_at last_ready_at ended_at ready_count has_issue issues amount_baht evidence')
    _match(s['id'],'[A-Za-z0-9_-]{1,96}');_match(s['camera_id'],'[A-Za-z0-9_-]{1,64}')
    if camera_id is not None and s['camera_id']!=camera_id:raise ValueError('invalid_payload')
    if (type(s['revision']) is not int or type(s['ready_count']) is not int
            or not 1<=s['ready_count']<=s['revision']<=1000000000):raise ValueError('invalid_payload')
    if value['event_id']!=f"{s['id']}:{s['revision']}" or (event_id is not None and value['event_id']!=event_id):
        raise ValueError('invalid_payload')
    if s['amount_baht'] is not None or type(s['has_issue']) is not bool:raise ValueError('invalid_payload')
    start,last=_date(s['started_at']),_date(s['last_ready_at'])
    if last<start:raise ValueError('invalid_payload')
    if s['state'] in ('open','interrupted'):
        if s['ended_at'] is not None:raise ValueError('invalid_payload')
    elif s['state']=='completed':
        if s['ended_at'] is None or _date(s['ended_at'])<last:raise ValueError('invalid_payload')
    else:raise ValueError('invalid_payload')
    if (value['event_type']=='refueling.completed')!=(s['state']=='completed') and value['event_type']!='refueling.updated':
        raise ValueError('invalid_payload')
    if not isinstance(s['issues'],list) or len(s['issues'])>3:raise ValueError('invalid_payload')
    codes=set()
    for issue in s['issues']:
        _keys(issue,'code detected_at')
        code=issue['code']
        if code not in ('ready_prompt_repeated','completion_not_detected','audio_source_interrupted') or code in codes or _date(issue['detected_at'])<start:
            raise ValueError('invalid_payload')
        if code=='audio_source_interrupted' and _date(issue['detected_at'])<last:raise ValueError('invalid_payload')
        codes.add(code)
    if (s['state']=='interrupted')!=('audio_source_interrupted' in codes):raise ValueError('invalid_payload')
    if s['has_issue']!=bool(codes) or (s['ready_count']>1)!=('ready_prompt_repeated' in codes):
        raise ValueError('invalid_payload')
    if value['event_type']=='refueling.attention_required' and not s['has_issue']:raise ValueError('invalid_payload')
    if not isinstance(s['evidence'],list) or len(s['evidence'])>64:raise ValueError('invalid_payload')
    ids=set()
    for image in s['evidence']:
        _keys(image,'id sha256 content_type byte_size captured_at')
        _match(image['id'],'[0-9a-f]{32}');_match(image['sha256'],'[0-9a-f]{64}')
        if (image['id'] in ids or image['content_type'] not in ('image/png','image/jpeg')
                or type(image['byte_size']) is not int or not 0<image['byte_size']<=MAX_IMAGE):raise ValueError('invalid_payload')
        _date(image['captured_at']);ids.add(image['id'])
    return value


def validate_observation(payload,camera_id=None,event_id=None):
    """Validate the separate immutable audio-evidence contract; not a session."""
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('invalid_payload')
            result[key]=value
        return result
    if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_JSON:raise ValueError('invalid_payload')
    value=json.loads(payload.decode('utf-8'),object_pairs_hook=unique,
                     parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid_payload')))
    _keys(value,'schema_version event_id event_type occurred_at observation')
    if type(value['schema_version']) is not int or value['schema_version']!=1 or value['event_type']!='refueling.observation':
        raise ValueError('invalid_payload')
    s=value['observation']
    _keys(s,'id camera_id kind reason observed_started_at observed_ended_at audio_group_id amount_baht evidence')
    _match(s['id'],'[0-9a-f]{32}')
    if int(s['id'],16)==0:raise ValueError('invalid_payload')
    _match(s['camera_id'],'[A-Za-z0-9_-]{1,64}')
    if camera_id is not None and s['camera_id']!=camera_id:raise ValueError('invalid_payload')
    if value['event_id']!=s['id']+':1' or (event_id is not None and value['event_id']!=event_id):raise ValueError('invalid_payload')
    reasons={'end':('completion_without_start',),
             'paid_ready':('awaiting_end_after_source_gap','expired_round_awaiting_completion')}
    if s['kind'] not in reasons or s['reason'] not in reasons[s['kind']]:raise ValueError('invalid_payload')
    if s['audio_group_id'] is not None:_match(s['audio_group_id'],'[A-Za-z0-9_.:-]{1,128}')
    if s['amount_baht'] is not None or s['evidence']!=[]:raise ValueError('invalid_payload')
    def instant(text):
        if not isinstance(text,str) or len(text)>40:raise ValueError('invalid_payload')
        parts=re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})'
                          r'(?:\.([0-9]+))?(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])',text)
        if parts is None:raise ValueError('invalid_payload')
        whole,fraction,zone=parts.groups()
        date=datetime.fromisoformat(whole+zone.replace('Z','+00:00'))
        delta=date-datetime(1970,1,1,tzinfo=timezone.utc)
        digits=fraction or ''
        # Never use timestamp floats or datetime's truncated microseconds for
        # evidence ordering/equality. The original fractional digits are exact.
        result=Fraction(delta.days*86400+delta.seconds)+Fraction(int(digits or '0'),10**len(digits))
        if result<=0:raise ValueError('invalid_payload')
        return result
    start=instant(s['observed_started_at'])
    if instant(s['observed_ended_at'])<start or instant(value['occurred_at'])!=start:raise ValueError('invalid_payload')
    return value
