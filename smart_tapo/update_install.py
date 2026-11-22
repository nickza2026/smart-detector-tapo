"""Fail-closed, single-volume executable transaction; never opens application data.

Internal helper flags must be dispatched before GUI/configuration imports. See
``docs/update-transaction.md`` for the integration contract and recovery limits.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
from . import updates
from .updates import UpdateError
from .update_windows import WindowsProcesses, ProcessGone, packaged_executable

CLAIM='.smart-tapo-update.claim'
FILES={'owner.json','plan.json','manifest.json','candidate.exe','helper.exe','backup.exe',
       'journal.json','armed.json','ready.json','helper.json','launch.json','child.json','completed.json'}
STATES={'prepared','armed','parent_exited','backed_up','swapped','ready','rolled_back','error'}
TEMP_FILES={'journal.json.tmp','launch.json.tmp','child.json.tmp'}
PLAN_FIELDS={'schema','token','target','old_hash','new_hash','size','version','current_version','parent','parent_bootloader','config'}


def current_version():
    from .diagnostics import APP_VERSION
    return int(APP_VERSION.lower().lstrip('v'))


def safe_path(value, *, exists=True):
    """Reject links (including every ancestor), device/UNC/ADS and ambiguous paths."""
    path=Path(value)
    text=str(path)
    if not path.is_absolute() or text.startswith(('\\\\','//')) or any(c in text for c in ('\x00','\r','\n')):
        raise UpdateError('Unsafe update path')
    if os.name=='nt':
        if ':' in text[2:] or any(p.endswith((' ','.')) for p in path.parts[1:]):
            raise UpdateError('Unsafe update path')
    if '..' in path.parts: raise UpdateError('Unsafe update path')
    for part in [*reversed(path.parents),path]:
        try: info=part.lstat()
        except FileNotFoundError:
            if part==path and not exists: continue
            raise UpdateError('Update path missing') from None
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise UpdateError('Linked update path rejected')
        if stat.S_ISREG(info.st_mode) and info.st_nlink!=1:
            raise UpdateError('Hardlinked update file rejected')
    return path


def digest(path):
    path=safe_path(path)
    if not path.is_file(): raise UpdateError('Expected regular update file')
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def _exe(path,expected=None,size=None):
    path=safe_path(path)
    if path.suffix.lower()!='.exe' or not path.is_file(): raise UpdateError('Executable path rejected')
    with path.open('rb') as stream:
        if stream.read(2)!=b'MZ': raise UpdateError('Executable header rejected')
    # MZ is only a format sanity check, NOT an authenticity decision.
    if size is not None and path.stat().st_size!=size: raise UpdateError('Executable size changed')
    actual=digest(path)
    if expected is not None and not hmac.compare_digest(actual,expected):
        raise UpdateError('Executable hash changed')
    return actual


def _read(path):
    path=safe_path(path)
    if path.stat().st_size>updates.MAX_METADATA: raise UpdateError('Transaction metadata too large')
    return updates._json(path.read_bytes())


def _write(path,obj,*,exclusive=False):
    safe_path(path,exists=path.exists())
    raw=json.dumps(obj,sort_keys=True,separators=(',',':')).encode('utf-8')
    if len(raw)>updates.MAX_METADATA: raise UpdateError('Transaction metadata too large')
    if exclusive:
        with path.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    else:
        temp=path.with_name(path.name+'.tmp')
        safe_path(temp,exists=False)
        with temp.open('xb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp,path)


def _same_path(a,b): return os.path.normcase(str(a))==os.path.normcase(str(b))


def _identity(value):
    if type(value) is not dict or set(value)!={'pid','path','created'}:
        raise UpdateError('Invalid process identity')
    if type(value['pid']) is not int or value['pid']<=0 or type(value['created']) is not int or value['created']<=0:
        raise UpdateError('Invalid process identity')
    safe_path(value['path'],exists=Path(value['path']).exists())
    return value


def _bind(processes,identity):
    _identity(identity)
    handle=processes.open(identity['pid'])
    try:
        actual=processes.identity(handle)
        if actual['pid']!=identity['pid'] or actual['created']!=identity['created'] or not _same_path(actual['path'],identity['path']):
            raise UpdateError('Process identity mismatch')
        return handle
    except BaseException:
        processes.close(handle); raise


def _config(value,stage,target):
    if value is None:return None
    path=safe_path(value)
    if path==target or stage==path or stage in path.parents or path.suffix.lower() in ('.exe','.pem','.key'):
        raise UpdateError('Unsafe restart configuration path')
    return str(path)


def prepare_update(current_exe,raw_manifest,verified_download,*,current_version=None,config_path=None,processes=None):
    """Copy into fresh owned stage and return absolute plan path; no GUI/data I/O.

    Does not stop the caller. Caller must start_helper and wait_for_arm before
    gracefully closing its GUI. The approved public key is never accepted as input.
    """
    version=globals()['current_version']()
    if current_version is not None and current_version!=version: raise UpdateError('Running version mismatch')
    manifest=updates.verify_manifest(raw_manifest,version,updates.PINNED_PUBLIC_KEY)
    target=safe_path(current_exe)
    if not _same_path(target,packaged_executable()): raise UpdateError('Not the running packaged executable')
    old_hash=_exe(target)
    source=safe_path(verified_download); _exe(source,manifest.sha256,manifest.size)
    processes=processes or WindowsProcesses()
    parent=processes.current(); _identity(parent)
    if not _same_path(parent['path'],target): raise UpdateError('Parent executable mismatch')
    token=secrets.token_hex(32)
    stage=target.parent/('.smart-tapo-update-'+secrets.token_hex(16))
    claim=target.parent/CLAIM
    if claim.exists():
        _archive_completed_claim(target,version)
    _write(claim,{'stage':str(stage),'token':token},exclusive=True)
    stage.mkdir(mode=0o700)
    _write(stage/'owner.json',{'schema':1,'token':token,'target':str(target)},exclusive=True)
    try:
        config=_config(config_path,stage,target)
        shutil.copyfile(target,stage/'helper.exe')
        shutil.copyfile(source,stage/'candidate.exe')
        _exe(stage/'helper.exe',old_hash); _exe(stage/'candidate.exe',manifest.sha256,manifest.size)
        for artifact in ('helper.exe','candidate.exe'):
            with (stage/artifact).open('r+b') as stream: os.fsync(stream.fileno())
        with (stage/'manifest.json').open('xb') as stream:
            stream.write(raw_manifest); stream.flush(); os.fsync(stream.fileno())
        plan={'schema':1,'token':token,'target':str(target),'old_hash':old_hash,
              'new_hash':manifest.sha256,'size':manifest.size,'version':manifest.version,
              'current_version':version,'parent':parent,
              'parent_bootloader':processes.bootloader(parent) if hasattr(processes,'bootloader') else None,'config':config}
        _write(stage/'plan.json',plan,exclusive=True)
        _journal(stage,token,'prepared')
        return stage/'plan.json'
    except BaseException:
        # Leave owned evidence and claim; cancellation is explicit and validated.
        raise


def _load(plan_path,token,*,archived=False):
    path=safe_path(plan_path)
    if path.name!='plan.json' or not re.fullmatch(r'\.smart-tapo-update-[0-9a-f]{32}',path.parent.name):
        raise UpdateError('Unowned update plan')
    stage=path.parent
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{64}',token): raise UpdateError('Invalid transaction token')
    plan=_read(path)
    if type(plan) is not dict or set(plan)!=PLAN_FIELDS or plan['schema']!=1 or not hmac.compare_digest(str(plan['token']),token):
        raise UpdateError('Invalid update plan')
    target=safe_path(plan['target'],exists=Path(plan['target']).exists())
    if target.parent!=stage.parent or target.suffix.lower()!='.exe': raise UpdateError('Target outside transaction volume')
    if _read(stage/'owner.json')!={'schema':1,'token':token,'target':str(target)}:
        raise UpdateError('Stage ownership mismatch')
    proof=stage/'completed.json' if archived else stage.parent/CLAIM
    if _read(proof)!={'stage':str(stage),'token':token}:
        raise UpdateError('Transaction claim mismatch')
    for name in ('old_hash','new_hash'):
        if type(plan[name]) is not str or not re.fullmatch('[0-9a-f]{64}',plan[name]): raise UpdateError('Invalid transaction hash')
    if type(plan['current_version']) is not int or type(plan['version']) is not int or type(plan['size']) is not int:
        raise UpdateError('Invalid transaction version')
    _identity(plan['parent'])
    if not _same_path(plan['parent']['path'],target): raise UpdateError('Invalid parent path')
    if plan['parent_bootloader'] is not None:
        _identity(plan['parent_bootloader'])
        if not _same_path(plan['parent_bootloader']['path'],target): raise UpdateError('Invalid bootloader path')
    _config(plan['config'],stage,target)
    journal=_read(stage/'journal.json')
    if type(journal) is not dict or set(journal)!={'token','state'} or journal['token']!=token or journal['state'] not in STATES:
        raise UpdateError('Invalid transaction journal')
    # Reject unknown artifacts. No general recursive cleanup ever follows this.
    for child in stage.iterdir():
        if child.name not in FILES and child.name not in TEMP_FILES:
            raise UpdateError('Unknown transaction artifact')
        safe_path(child)
        if not child.is_file(): raise UpdateError('Non-file transaction artifact')
    return stage,plan,journal['state']


def _completed_proof(stage,plan,state,*,target_required):
    if state!='ready': raise UpdateError('Prior update is not terminal ready')
    if any((stage/name).exists() for name in TEMP_FILES):
        raise UpdateError('Ambiguous completed transaction')
    manifest=updates.verify_manifest((stage/'manifest.json').read_bytes(),plan['current_version'],updates.PINNED_PUBLIC_KEY)
    if (manifest.version,manifest.sha256,manifest.size)!=(plan['version'],plan['new_hash'],plan['size']):
        raise UpdateError('Completed manifest mismatch')
    if target_required:
        if current_version()!=plan['version']: raise UpdateError('Completed running version mismatch')
        _exe(Path(plan['target']),plan['new_hash'],plan['size'])
    _exe(stage/'backup.exe',plan['old_hash'])
    ack=_read(stage/'ready.json')
    if type(ack) is not dict or set(ack)!={'token','child'} or ack['token']!=plan['token']:
        raise UpdateError('Completed startup proof mismatch')
    _identity(ack['child'])
    if not _same_path(ack['child']['path'],plan['target']): raise UpdateError('Completed startup image mismatch')


def _archive_completed_claim(target,version):
    """Retain old backup and atomically retire ONLY a verified healthy claim.

    Windows rename is no-replace: competing release attempts cannot replace the
    completed proof or accidentally remove a newer active claim. A crash before
    rename retains the claim; after rename the archived proof is authoritative.
    """
    claim=target.parent/CLAIM
    entry=_read(claim)
    if type(entry) is not dict or set(entry)!={'stage','token'}: raise UpdateError('Invalid prior claim')
    stage,plan,state=_load(Path(entry['stage'])/'plan.json',entry['token'])
    if not _same_path(plan['target'],target) or plan['version']!=version:
        raise UpdateError('Prior completed target mismatch')
    _completed_proof(stage,plan,state,target_required=True)
    if os.name!='nt': raise UpdateError('Atomic claim archival requires Windows no-replace rename')
    os.rename(claim,stage/'completed.json')


def _journal(stage,token,state): _write(stage/'journal.json',{'token':token,'state':state})


def start_helper(plan_path,token=None,*,processes=None):
    """Launch trusted self-copy hidden. This is NOT an authorization to exit yet."""
    if token is None: token=_read(Path(plan_path))['token']
    stage,plan,state=_load(plan_path,token)
    if state!='prepared' or (stage/'helper.json').exists() or (stage/'armed.json').exists():
        raise UpdateError('Helper already started or stale handshake')
    if not _same_path(packaged_executable(),plan['target']): raise UpdateError('Not the original application')
    _exe(plan['target'],plan['old_hash']); _exe(stage/'helper.exe',plan['old_hash'])
    processes=processes or WindowsProcesses()
    _write(stage/'launch.json',{'token':token,'helper':None},exclusive=True)
    child=processes.launch(stage/'helper.exe',['--update-helper',str(stage/'plan.json'),'--update-token',token],hidden=True,cwd=stage.parent)
    identity=processes.identity(processes.child_handle(child))
    _write(stage/'launch.json',{'token':token,'helper':identity})
    return child


def wait_for_arm(plan_path,token=None,*,processes=None,helper=None,timeout=15):
    """Only true after helper has bound the exact old process HANDLE."""
    if token is None: token=_read(Path(plan_path))['token']
    stage,plan,_=_load(plan_path,token)
    processes=processes or WindowsProcesses()
    if helper is None: raise UpdateError('Owned helper handle required')
    handle=processes.child_handle(helper)
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if not processes.alive(handle): raise UpdateError('Helper stopped before arm')
        if (stage/'armed.json').exists():
            ack=_read(stage/'armed.json')
            expected=ack.get('helper')
            _identity(expected)
            worker=processes.bind_startup(helper,expected)
            try:
                if ack!={'token':token,'parent':plan['parent'],'helper':processes.identity(worker)} or not _same_path(expected['path'],stage/'helper.exe'):
                    raise UpdateError('Invalid helper arm acknowledgement')
                return True
            finally: processes.close(worker)
        time.sleep(.05)
    raise UpdateError('Helper arm timed out; keep application open')


def _restart(processes,target,plan,stage=None,token=None):
    args=['--update-restart']
    if plan['config'] is not None: args+=['--config',plan['config']]
    if stage is not None: args+=['--update-startup-plan',str(stage/'plan.json'),'--update-token',token]
    return processes.launch(target,args,hidden=False,cwd=target.parent)


def acknowledge_startup(plan_path,token,*,processes=None):
    """Call ONLY after real normal GUI initialized with automatic work suppressed."""
    stage,plan,state=_load(plan_path,token)
    if state!='swapped': raise UpdateError('Unexpected startup acknowledgement')
    if current_version()!=plan['version']: raise UpdateError('Startup running build version mismatch')
    manifest=updates.verify_manifest((stage/'manifest.json').read_bytes(),plan['current_version'],updates.PINNED_PUBLIC_KEY)
    if (manifest.version,manifest.sha256,manifest.size)!=(plan['version'],plan['new_hash'],plan['size']):
        raise UpdateError('Startup signed manifest mismatch')
    if not _same_path(packaged_executable(),plan['target']): raise UpdateError('Startup executable mismatch')
    _exe(Path(plan['target']),plan['new_hash'],plan['size'])
    processes=processes or WindowsProcesses()
    identity=processes.current()
    if not _same_path(identity['path'],plan['target']): raise UpdateError('Startup image mismatch')
    _write(stage/'ready.json',{'token':token,'child':identity},exclusive=True)


def _rollback(stage,plan,processes):
    target=Path(plan['target']); backup=stage/'backup.exe'
    _exe(backup,plan['old_hash'])
    if target.exists():
        _exe(target,plan['new_hash'],plan['size'])
    # Atomic replacement only when the existing target is our verified candidate.
    os.replace(backup,target)
    _exe(target,plan['old_hash'])
    _journal(stage,plan['token'],'rolled_back')
    _restart(processes,target,plan)


def _verify_helper(stage,plan):
    if plan['current_version']!=current_version(): raise UpdateError('Helper build version mismatch')
    if not _same_path(packaged_executable(),stage/'helper.exe'): raise UpdateError('Not trusted helper executable')
    _exe(stage/'helper.exe',plan['old_hash'])
    raw_path=safe_path(stage/'manifest.json')
    if raw_path.stat().st_size>updates.MAX_METADATA: raise UpdateError('Manifest too large')
    manifest=updates.verify_manifest(raw_path.read_bytes(),current_version(),updates.PINNED_PUBLIC_KEY)
    if (manifest.version,manifest.sha256,manifest.size)!=(plan['version'],plan['new_hash'],plan['size']):
        raise UpdateError('Plan and signed manifest mismatch')


def apply_update(plan_path,token,*,processes=None,parent_timeout=120,health_timeout=60,grace=3):
    """Hidden helper entry point. Zero means a healthy GUI committed; no user data I/O."""
    processes=processes or WindowsProcesses()
    parent_handle=None; bootloader_handle=None
    try:
        stage,plan,state=_load(plan_path,token)
        _verify_helper(stage,plan)
        if state!='prepared': raise UpdateError('Use explicit recovery for interrupted transaction')
        if any((stage/name).exists() for name in ('ready.json','armed.json','helper.json','child.json','backup.exe')):
            raise UpdateError('Stale transaction artifact')
        _exe(Path(plan['target']),plan['old_hash'])
        _exe(stage/'candidate.exe',plan['new_hash'],plan['size'])
        parent_handle=_bind(processes,plan['parent'])
        if plan['parent_bootloader'] is not None:
            bootloader_handle=_bind(processes,plan['parent_bootloader'])
            if processes.bootloader(plan['parent'])!=plan['parent_bootloader']:
                raise UpdateError('Old bootloader ancestry mismatch')
        own=processes.current()
        if not _same_path(own['path'],stage/'helper.exe'): raise UpdateError('Helper identity mismatch')
        _write(stage/'helper.json',{'token':token,'helper':own},exclusive=True)
        _journal(stage,token,'armed')
        _write(stage/'armed.json',{'token':token,'parent':plan['parent'],'helper':own},exclusive=True)
        if not processes.wait(parent_handle,parent_timeout): raise UpdateError('Parent still running; update not applied')
        if bootloader_handle is not None and not processes.wait(bootloader_handle,parent_timeout):
            raise UpdateError('Old bootloader still running; update not applied')
        _journal(stage,token,'parent_exited')
        target=Path(plan['target'])
        _exe(target,plan['old_hash']); _exe(stage/'candidate.exe',plan['new_hash'],plan['size'])
        if (stage/'backup.exe').exists(): raise UpdateError('Unexpected existing backup')
        os.replace(target,stage/'backup.exe')
        _journal(stage,token,'backed_up')
        os.replace(stage/'candidate.exe',target)
        _journal(stage,token,'swapped')
        child=None; worker=None
        try:
            child=_restart(processes,target,plan,stage,token)
            handle=processes.child_handle(child)
            identity=processes.identity(handle)
            _write(stage/'child.json',{'token':token,'child':identity},exclusive=True)
            if not _same_path(identity['path'],target): raise UpdateError('New child image mismatch')
            end=time.monotonic()+health_timeout
            healthy=False
            while time.monotonic()<end:
                if not processes.alive(handle): break
                if (stage/'ready.json').exists():
                    ack=_read(stage/'ready.json')
                    if type(ack) is not dict or set(ack)!={'token','child'} or ack['token']!=token:
                        raise UpdateError('Startup acknowledgement token mismatch')
                    _identity(ack['child'])
                    worker=processes.bind_startup(child,ack['child'])
                    if not _same_path(processes.identity(worker)['path'],target): raise UpdateError('Startup worker image mismatch')
                    _write(stage/'child.json',{'token':token,'child':processes.identity(worker)})
                    if not processes.wait(worker,grace) and processes.alive(handle): healthy=True
                    break
                time.sleep(.05)
            if not healthy: raise UpdateError('New GUI did not become healthy')
            _exe(target,plan['new_hash'],plan['size'])
            _journal(stage,token,'ready')
            # Keep verified backup and owned claim until explicit artifact cleanup.
            return 0
        except Exception:
            if worker is not None: processes.kill(worker)
            if child is not None: processes.kill_launch(child)
            _rollback(stage,plan,processes)
            return 1
        finally:
            if worker is not None: processes.close(worker)
    except (OSError,ValueError,TypeError,KeyError,UpdateError):
        # Do not guess or remove artifacts on ambiguous failure. recover_update can
        # reconcile crashes after either rename using cryptographic identities.
        return 2
    finally:
        if parent_handle is not None: processes.close(parent_handle)
        if bootloader_handle is not None: processes.close(bootloader_handle)


def recover_update(plan_path,token,*,processes=None):
    """Explicit trusted helper recovery. Never swaps forward on uncertain history."""
    stage,plan,state=_load(plan_path,token)
    _verify_helper(stage,plan)
    processes=processes or WindowsProcesses()
    # A live parent/helper makes recovery unsafe; PID reuse is not ownership.
    identities=[plan['parent']]+([plan['parent_bootloader']] if plan['parent_bootloader'] is not None else [])
    for filename,key in (('helper.json','helper'),('launch.json','helper'),('child.json','child')):
        if (stage/filename).exists():
            entry=_read(stage/filename)
            if entry.get('token')!=token or entry.get(key) is None: raise UpdateError('Ambiguous process reservation')
            identities.append(_identity(entry[key]))
    for identity in identities:
        if identity==processes.current(): continue
        try: handle=processes.open(identity['pid'])
        except ProcessGone: continue
        try:
            if processes.identity(handle)==identity and processes.alive(handle): raise UpdateError('Transaction process still live')
        finally: processes.close(handle)
    pending=stage/'journal.json.tmp'
    if pending.exists():
        value=_read(pending)
        if type(value) is not dict or set(value)!={'token','state'} or value['token']!=token or value['state'] not in STATES:
            raise UpdateError('Ambiguous interrupted journal write')
        # Authoritative journal plus verified file layout decide recovery, not
        # a partially prepared replacement journal. Only this owned temp is removed.
        pending.unlink()
    target=Path(plan['target']); backup=stage/'backup.exe'
    if state=='ready':
        _exe(target,plan['new_hash'],plan['size']); _exe(backup,plan['old_hash']); return 'ready'
    if backup.exists():
        _exe(backup,plan['old_hash'])
        if target.exists(): _exe(target,plan['new_hash'],plan['size'])
        _rollback(stage,plan,processes)
        return 'rolled_back'
    _exe(target,plan['old_hash'])
    if state not in ('prepared','armed','parent_exited','rolled_back'):
        raise UpdateError('Recovery layout inconsistent; manual intervention required')
    _journal(stage,token,'rolled_back')
    return 'rolled_back'


def cancel_update(plan_path,token,*,processes=None,discard_healthy_backup=False):
    """Remove ONLY whitelisted owned stage files, never an unknown/live transaction."""
    archived=(Path(plan_path).parent/'completed.json').exists()
    stage,plan,state=_load(plan_path,token,archived=archived)
    if archived: _completed_proof(stage,plan,state,target_required=False)
    processes=processes or WindowsProcesses()
    if state not in ('prepared','rolled_back','ready'): raise UpdateError('Active or interrupted update cannot be cancelled')
    for filename in ('launch.json','helper.json'):
        if not (stage/filename).exists(): continue
        entry=_read(stage/filename)
        if entry.get('token')!=token or entry.get('helper') is None: raise UpdateError('Helper launch reservation unresolved')
        identity=_identity(entry['helper'])
        try: handle=processes.open(identity['pid'])
        except ProcessGone: handle=None
        if handle is not None:
            try:
                if processes.identity(handle)==identity and processes.alive(handle): raise UpdateError('Helper still live')
            finally: processes.close(handle)
    target=Path(plan['target'])
    if not archived: _exe(target,plan['new_hash'] if state=='ready' else plan['old_hash'])
    # Backup retention is deliberate: successful transactions require a separate
    # operator decision before deleting the last old binary.
    if (stage/'backup.exe').exists():
        if state!='ready' or discard_healthy_backup is not True:
            raise UpdateError('Verified backup retained; cleanup requires operator approval')
        _exe(stage/'backup.exe',plan['old_hash'])
    for path in list(stage.iterdir()):
        safe_path(path); path.unlink()
    stage.rmdir()
    if not archived:
        claim=target.parent/CLAIM
        if _read(claim)!={'stage':str(stage),'token':token}: raise UpdateError('Claim changed during cancellation')
        claim.unlink()
