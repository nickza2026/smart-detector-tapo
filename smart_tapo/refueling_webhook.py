"""Explicit outbound image + event delivery. No I/O on import/construction."""
from __future__ import annotations

from dataclasses import dataclass,field
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib import error,parse,request
from .diagnostics import APP_VERSION
from .refueling_api_log import MAX_EXCERPT,excerpt
from .refueling_contract import MAX_IMAGE,MAX_JSON,validate_event



def checked_path(path,root):
    path,root=Path(path).absolute(),Path(root).absolute()
    try:path.relative_to(root)
    except ValueError:raise ValueError('unsafe_media') from None
    for part in (path,*path.parents):
        if part.is_symlink() or part.is_junction():raise ValueError('unsafe_media')
    if not path.is_file():raise ValueError('media_missing')
    return path


@dataclass(frozen=True)
class WebhookTarget:
    base_url:str=field(repr=False)
    token:str=field(repr=False)
    camera_id:str
    allow_loopback_http:bool=False

    def __post_init__(self):
        if not isinstance(self.base_url,str) or len(self.base_url)>2048:
            raise ValueError('invalid_destination')
        if any(c.isspace() or ord(c)<32 for c in self.base_url):raise ValueError('invalid_destination')
        url=parse.urlsplit(self.base_url)
        try:
            local=ipaddress.ip_address(url.hostname or '').is_loopback
            port=url.port
        except ValueError:
            local=False
            try:port=url.port
            except ValueError:raise ValueError('invalid_destination') from None
        if (not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment
                or url.scheme not in ('https','http') or '%' in url.netloc
                or any(p in ('.','..') for p in url.path.split('/'))
                or (url.scheme!='https' and not (self.allow_loopback_http is True and local))):
            raise ValueError('invalid_destination')
        if not isinstance(self.camera_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.camera_id):
            raise ValueError('invalid_camera')
        if (not isinstance(self.token,str) or not 16<=len(self.token)<=4096
                or any(not 33<=ord(c)<=126 for c in self.token)):
            raise ValueError('invalid_token')
        object.__setattr__(self,'base_url',self.base_url.rstrip('/'))

    @property
    def destination_key(self):
        value=self.base_url+'\0'+self.camera_id+'\0'+hashlib.sha256(self.token.encode()).hexdigest()
        return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class DeliveryResult:
    status:str
    error_code:str|None=None
    retry_after:float|None=None
    invalidate_images:bool=False


USER_AGENT='SmartTapo/'+APP_VERSION+' (refueling-api)'


def _firewall_block(response,body):
    """A CDN/WAF refusal reached no application, so it says nothing about the API key."""
    server=str(response.headers.get('Server','')).lower() if response.headers is not None else ''
    return server=='cloudflare' and isinstance(body,(bytes,bytearray)) and b'1010' in body


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class RefuelingWebhookClient:
    def __init__(self,target,media_root,*,opener=None,timeout=10,monotonic=time.monotonic,log=None):
        if not isinstance(target,WebhookTarget):raise ValueError('invalid_target')
        if type(timeout) not in (int,float) or not 0<timeout<=30:raise ValueError('invalid_timeout')
        self.target,self.media_root=target,Path(media_root).absolute()
        # Scoped policy: never inherit arbitrary environment proxy or follow auth redirects.
        self.opener=opener or request.build_opener(request.ProxyHandler({}),_NoRedirect())
        self.timeout,self.monotonic=timeout,monotonic
        self.log=log

    def record(self,result,**attempt):
        """Diagnostics must never change or break a delivery outcome."""
        if self.log is None:return
        try:
            for name in ('response','error_detail'):
                if attempt.get(name):attempt[name]=excerpt(attempt[name],self.target.token)
            self.log(dict(attempt,status=result.status,error_code=result.error_code))
        except Exception:
            pass

    def _send(self,method,suffix,body,content_type,key,extra=None):
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_:-]{1,140}',key):
            return DeliveryResult('failed','invalid_idempotency_key')
        headers={'Authorization':'Bearer '+self.target.token,'X-Camera-ID':self.target.camera_id,
                 'Idempotency-Key':key,'Content-Type':content_type,'Accept':'application/json',
                 # Cloudflare Browser Integrity Check bans urllib's default agent (error 1010).
                 'User-Agent':USER_AGENT}
        headers.update(extra or {})
        req=request.Request(self.target.base_url+'/v1/refueling/'+suffix,data=body,headers=headers,method=method)
        attempt=dict(method=method,url=req.full_url,key=key,http_status=None,response=None,error_detail=None)
        started=time.perf_counter()
        result=self._exchange(req,suffix,headers,attempt)
        self.record(result,elapsed_ms=round((time.perf_counter()-started)*1000),**attempt)
        return result

    def _exchange(self,req,suffix,headers,attempt):
        response=None
        deadline=self.monotonic()+self.timeout
        try:
            try:response=self.opener.open(req,timeout=self.timeout)
            except error.HTTPError as failure:response=failure
            status=response.code
            attempt['http_status']=status
            if 300<=status<400:
                attempt['error_detail']='redirect to '+str(response.headers.get('Location',''))
                return DeliveryResult('failed','redirect_disallowed')
            if status==429 or status>=500 or status in (401,403):
                try:attempt['response']=response.read(MAX_EXCERPT)
                except Exception:pass
            if status==429:
                try:retry=max(1,min(3600,float(response.headers.get('Retry-After','5'))))
                except (TypeError,ValueError):retry=5
                return DeliveryResult('retry','rate_limited',retry)
            if status>=500:return DeliveryResult('retry','receiver_unavailable')
            if status==403 and _firewall_block(response,attempt['response']):
                return DeliveryResult('failed','receiver_firewall_blocked')
            if status in (401,403):return DeliveryResult('failed','receiver_auth_rejected')
            data=bytearray()
            while True:
                if self.monotonic()>deadline:raise TimeoutError
                read=getattr(response,'read1',response.read)
                chunk=read(min(4096,MAX_JSON+1-len(data)))
                if not chunk:break
                data.extend(chunk)
                attempt['response']=data
                if len(data)>MAX_JSON:return DeliveryResult('failed','receiver_response_too_large')
            try:
                document=json.loads(data.decode('utf-8'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            except (ValueError,UnicodeError,RecursionError):return DeliveryResult('failed','invalid_receiver_response')
            if not isinstance(document,dict):return DeliveryResult('failed','invalid_receiver_response')
            if status==409 and document.get('code')=='images_missing':
                return DeliveryResult('retry','receiver_images_missing',5,True)
            if status not in (200,201):return DeliveryResult('failed','receiver_rejected')
            if suffix.startswith('images/'):
                ok=(document.get('stored') is True and document.get('image_id')==attempt['key']
                    and document.get('sha256')==headers.get('X-Content-SHA256'))
            else:ok=document.get('accepted') is True and document.get('event_id')==attempt['key']
            return DeliveryResult('delivered') if ok else DeliveryResult('failed','invalid_receiver_ack')
        except (OSError,error.URLError,http.client.HTTPException) as failure:
            reason=getattr(failure,'reason',None)
            attempt['error_detail']=type(failure).__name__+': '+str(reason if reason is not None else failure)
            return DeliveryResult('retry','transport_unconfirmed')
        finally:
            if response is not None:response.close()

    def upload_image(self,image):
        try:
            identifier=image['id']
            if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError
            if image['content_type'] not in ('image/png','image/jpeg'):raise ValueError
            path=checked_path(image['path'],self.media_root)
            if not 0<path.stat().st_size<=MAX_IMAGE:raise ValueError
            with path.open('rb') as source:data=source.read(MAX_IMAGE+1)
            digest=hashlib.sha256(data).hexdigest()
            if (len(data)>MAX_IMAGE or len(data)!=image['byte_size'] or digest!=image['sha256']):raise ValueError
        except (KeyError,TypeError,ValueError,OSError):
            return _local(self,DeliveryResult('failed','invalid_or_missing_media'),image.get('id') if isinstance(image,dict) else None)
        return self._send('PUT','images/'+identifier,data,image['content_type'],identifier,
                          {'X-Content-SHA256':digest})

    def send_event(self,payload,event_id):
        if not isinstance(payload,bytes) or len(payload)>MAX_JSON:return _local(self,DeliveryResult('failed','invalid_payload'),event_id)
        try:
            validate_event(payload,self.target.camera_id,event_id)
        except (KeyError,ValueError,TypeError,UnicodeError,RecursionError):
            return _local(self,DeliveryResult('failed','invalid_payload'),event_id)
        return self._send('POST','events',payload,'application/json',event_id)


def _local(client,result,key):
    """A failure decided before any request is still an attempt the operator must see."""
    record=getattr(client,'record',None)
    if record is not None:record(result,method=None,url=None,key=key if isinstance(key,str) else None)
    return result


def dispatch_one(store,client,*,clock=time.time):
    """One attempt, image cache persisted per destination. No implicit loop/retry."""
    destination=client.target.destination_key
    store.cleanup(now=clock())  # Expired content must go even when no due job remains.
    jobs=store.pending_jobs(destination,limit=1)
    if not jobs:return None
    job=jobs[0]
    try:
        value=validate_event(job['payload'],client.target.camera_id,job['event_id'])
        manifest={image['id']:image['sha256'] for image in value['session']['evidence']}
        actual={image['id']:image['sha256'] for image in job['images']}
        if manifest!=actual or len(actual)!=len(job['images']):raise ValueError
    except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):
        store.mark_delivery(job['event_id'],'failed',error_code='invalid_payload')
        return _local(client,DeliveryResult('failed','invalid_payload'),job['event_id'])
    if job['destination_key']!=destination:return _local(client,DeliveryResult('failed','destination_mismatch'),job['event_id'])
    expired=DeliveryResult('failed','expired_before_delivery_confirmation')
    if clock()>=job['expires_at']:
        store.cleanup(now=clock())
        return _local(client,expired,job['event_id'])
    result=None
    for image in job['images']:
        if clock()>=job['expires_at']:
            store.cleanup(now=clock())
            return _local(client,expired,job['event_id'])
        if not store.image_uploaded(image['id'],destination):
            result=client.upload_image(image)
            if result.status!='delivered':break
            store.mark_image_uploaded(image['id'],destination)
    if result is None or result.status=='delivered':
        if clock()>=job['expires_at']:
            store.cleanup(now=clock())
            return _local(client,expired,job['event_id'])
        result=client.send_event(job['payload'],job['event_id'])
    if result.invalidate_images:
        if job['attempts']>=2:result=DeliveryResult('failed','receiver_media_contract_mismatch')
        else:store.forget_image_uploads(destination)
    delay=result.retry_after or min(3600,5*(2**min(job['attempts'],10)))
    store.mark_delivery(job['event_id'],result.status,retry_at=clock()+delay if result.status=='retry' else None,
                        error_code=result.error_code)
    return result
