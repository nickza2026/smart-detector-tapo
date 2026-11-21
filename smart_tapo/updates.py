"""Public release discovery and signed download boundary (no ambient credentials).

This module never opens application configuration or a business database.
Trust roots are shipped in code, never learned from release metadata.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPOSITORY='nickza2026/smart-detector-tapo'
API_URL='https://api.github.com/repos/'+REPOSITORY+'/releases/latest'
RELEASES_URL='https://github.com/'+REPOSITORY+'/releases'
# Created locally after explicit operator approval, never learned from Releases.
# Only the public half is shipped; the private key is current-user DPAPI outside the repository.
PINNED_PUBLIC_KEY=bytes.fromhex('fc4f1f7eb74b1adfbc9e6047e2863d8071b6d17ce79dd230495e0d2fd6f93644')
MAX_EXE=512*1024*1024
MAX_METADATA=256*1024
BUSINESS_FLAGS=('workers','capture','listener','calibration','offline','webhook',
                'open_round','interrupted_round','outstanding_writes')

class UpdateError(Exception):
    """Safe fixed diagnostic: do not include URLs, response bodies or raw errors."""

class RateLimited(UpdateError):
    def __init__(self,seconds):
        super().__init__('GitHub rate limit; retry later')
        self.seconds=seconds


@dataclass(frozen=True)
class Manifest:
    version:int
    filename:str
    size:int
    sha256:str
    signature:bytes
    payload:bytes


def _pairs(pairs):
    out={}
    for key,value in pairs:
        if key in out:raise UpdateError('Duplicate JSON field')
        out[key]=value
    return out


def _json(raw):
    if len(raw)>MAX_METADATA:raise UpdateError('Metadata too large')
    try:return json.loads(raw,object_pairs_hook=_pairs)
    except (ValueError,UnicodeError):raise UpdateError('Malformed metadata') from None


def parse_manifest(raw,current):
    obj=_json(raw)
    fields={'schema','repository','version','filename','size','sha256','signature'}
    if type(obj) is not dict or set(obj)!=fields:raise UpdateError('Manifest schema mismatch')
    if type(obj['schema']) is not int or obj['schema']!=1:raise UpdateError('Unknown schema')
    v=obj['version']
    if type(v) is not int or not current<v<=2147483647:raise UpdateError('Version replay or downgrade')
    if obj['repository']!=REPOSITORY:raise UpdateError('Repository mismatch')
    if obj['filename']!='SmartTapo-V'+str(v)+'.exe':raise UpdateError('Executable name mismatch')
    if type(obj['size']) is not int or not 0<obj['size']<=MAX_EXE:raise UpdateError('Invalid executable size')
    if type(obj['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',obj['sha256']):raise UpdateError('Invalid digest')
    try:
        signature=base64.b64decode(obj['signature'],validate=True)
        if len(signature)!=64:raise ValueError()
    except (ValueError,TypeError):raise UpdateError('Missing or malformed signature') from None
    payload=json.dumps({k:v for k,v in obj.items() if k!='signature'},sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
    return Manifest(v,obj['filename'],obj['size'],obj['sha256'],signature,payload)


def verify_manifest(raw,current,public_key=PINNED_PUBLIC_KEY):
    m=parse_manifest(raw,current)
    if public_key is None:raise UpdateError('No approved pinned signing key; installation disabled')
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(public_key).verify(m.signature,m.payload)
    except ImportError:raise UpdateError('Signature verifier unavailable; installation disabled') from None
    except Exception:raise UpdateError('Signature verification failed') from None
    return m


def validate_url(url,*,api=False):
    try:
        p=urllib.parse.urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.fragment:raise ValueError()
        if api:
            if url!=API_URL:raise ValueError()
        elif p.hostname=='github.com':
            if not p.path.startswith('/'+REPOSITORY+'/releases/download/'):raise ValueError()
            if p.query:raise ValueError()
        elif p.hostname not in ('release-assets.githubusercontent.com','objects.githubusercontent.com'):raise ValueError()
        if any(x in p.path for x in ('\\','/../','/./','%')):raise ValueError()
    except (ValueError,TypeError):raise UpdateError('Release URL rejected') from None
    return url


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch_public(url,limit,stop=None,*,api=False):
    validate_url(url,api=api)
    # Explicitly ignore proxy environment and auth handlers/token/netrc.
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_Redirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req=urllib.request.Request(url,headers={'User-Agent':'SmartTapo-public-updater','Accept':'application/vnd.github+json' if api else 'application/octet-stream'})
    deadline=time.monotonic()+120
    try:
        with opener.open(req,timeout=10) as response:
            length=response.headers.get('Content-Length')
            if length and (not length.isdigit() or int(length)>limit):raise UpdateError('Response size rejected')
            chunks=[];total=0
            while True:
                if (stop and stop.is_set()) or time.monotonic()>deadline:raise UpdateError('Update cancelled or timed out')
                chunk=response.read(min(65536,limit-total+1))
                if not chunk:break
                total+=len(chunk)
                if total>limit:raise UpdateError('Response too large')
                chunks.append(chunk)
            return b''.join(chunks)
    except urllib.error.HTTPError as error:
        if error.code in (403,429):
            seconds=3600
            for name in ('Retry-After','X-RateLimit-Reset'):
                value=error.headers.get(name,'')
                if value.isdigit() and len(value)<=12:
                    seconds=max(seconds,int(value)-(int(time.time()) if name=='X-RateLimit-Reset' else 0))
            raise RateLimited(seconds) from None
        raise UpdateError('Release request failed') from None
    except (OSError,ValueError):raise UpdateError('Release connection failed') from None


class ReleaseClient:
    def __init__(self,*,fetch=fetch_public,clock=time.monotonic):
        self.fetch=fetch;self.clock=clock;self.next_check=0
        self.lock=threading.Lock()

    def check(self,current,stop=None):
        with self.lock:
            if self.clock()<self.next_check:raise UpdateError('Check cooldown active; retry later')
            # One hour minimum, including errors; no aggressive automatic retries.
            self.next_check=self.clock()+3600
        try:obj=_json(self.fetch(API_URL,MAX_METADATA,stop,api=True))
        except RateLimited as error:
            with self.lock:self.next_check=max(self.next_check,self.clock()+error.seconds)
            raise
        if type(obj) is not dict or obj.get('draft') is not False or obj.get('prerelease') is not False:raise UpdateError('Invalid public release')
        tag=obj.get('tag_name')
        if type(tag) is not str or not re.fullmatch('V[1-9][0-9]{0,9}',tag):raise UpdateError('Invalid release version')
        version=int(tag[1:])
        if version<=current:return None
        assets=obj.get('assets')
        if type(assets) is not list or len(assets)>32:raise UpdateError('Invalid release assets')
        found={}
        for a in assets:
            if type(a) is not dict:raise UpdateError('Invalid release asset')
            name=a.get('name')
            if name in ('SmartTapo-update.json','SmartTapo-'+tag+'.exe'):
                if name in found:raise UpdateError('Duplicate release asset')
                url=a.get('browser_download_url')
                validate_url(url)
                expected='https://github.com/'+REPOSITORY+'/releases/download/'+tag+'/'+name
                if url!=expected:raise UpdateError('Asset release mismatch')
                found[name]=url
        if len(found)!=2:raise UpdateError('Signed release assets missing')
        raw=self.fetch(found['SmartTapo-update.json'],MAX_METADATA,stop)
        m=verify_manifest(raw,current)
        if m.version!=version:raise UpdateError('Tag and signed version mismatch')
        return m,found[m.filename]


def download(manifest,url,destination,stop,*,fetch=fetch_public):
    """Destination MUST be an exclusively owned updater temporary directory."""
    path=Path(destination)
    # Remove a previous owned artifact before any operation so failure cannot
    # leave an old success artifact available for handoff.
    if path.is_symlink():raise UpdateError('Download path rejected')
    path.unlink(missing_ok=True)
    try:
        if stop.is_set():raise UpdateError('Update cancelled')
        raw=fetch(url,manifest.size,stop)
        if stop.is_set():raise UpdateError('Update cancelled')
        if len(raw)!=manifest.size or hashlib.sha256(raw).hexdigest()!=manifest.sha256:raise UpdateError('Executable integrity failure')
        with path.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def install_allowed(state):
    return type(state) is dict and set(state)==set(BUSINESS_FLAGS) and all(state[k] is False for k in BUSINESS_FLAGS)
