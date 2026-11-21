"""Disposable synthetic MZ fixtures test transaction mechanics, not PE trust.
No keys, application configuration, native programs, or hardware are used.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from smart_tapo import update_install as ui
from smart_tapo.updates import Manifest, UpdateError
REAL_VERIFY=ui.updates.verify_manifest


class Processes:
    def __init__(self,target):
        self.parent={'pid':11,'created':100,'path':str(target)}
        self.me=self.parent.copy(); self.live={11:True}; self.identities={11:self.parent.copy()}
        self.launched=[]; self.killed=[]; self.on_launch=None; self.busy=False
    def current(self): return self.me.copy()
    def open(self,pid,**kw):
        if pid not in self.identities: raise ui.ProcessGone('gone')
        return pid
    def identity(self,h): return self.identities[h].copy()
    def close(self,h): pass
    def alive(self,h): return self.live.get(h,False)
    def wait(self,h,seconds):
        if h==11 and not self.busy: self.live[h]=False
        return not self.alive(h)
    def kill(self,h): self.killed.append(h); self.live[h]=False
    def child_handle(self,p): return p
    def kill_launch(self,p): self.kill(p)
    def bind_startup(self,p,identity):
        actual=self.identity(p)
        if actual!=identity: raise UpdateError('startup spoof')
        return p
    def launch(self,exe,args,**kwargs):
        pid=50+len(self.launched); self.launched.append((Path(exe),args,kwargs))
        self.identities[pid]={'pid':pid,'created':pid*100,'path':str(exe)}; self.live[pid]=True
        if self.on_launch: self.on_launch(pid,args)
        return pid


class Transactions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.target=self.root/'app.exe'; self.source=self.root/'new.exe'
        self.target.write_bytes(b'MZtrusted-old'); self.source.write_bytes(b'MZsigned-new')
        self.proc=Processes(self.target)
        self.m=Manifest(45,'SmartTapo-V45.exe',len(self.source.read_bytes()),ui.digest(self.source),b'x'*64,b'{}')
        self.verify=patch.object(ui.updates,'verify_manifest',return_value=self.m).start()
        self.addCleanup(patch.stopall)
        patch.object(ui,'current_version',return_value=44).start()
        self.packaged=patch.object(ui,'packaged_executable',return_value=self.target).start()
    def prepare(self):
        plan=ui.prepare_update(self.target,b'{}',self.source,processes=self.proc)
        data=json.loads(plan.read_text()); self.plan=plan; self.token=data['token']; self.stage=plan.parent
        self.data=data
        return plan
    def helper(self):
        self.packaged.return_value=self.stage/'helper.exe'
        self.proc.me={'pid':22,'created':220,'path':str(self.stage/'helper.exe')}
        self.proc.identities[22]=self.proc.me.copy(); self.proc.live[22]=True
    def apply(self,**kw):
        self.helper()
        return ui.apply_update(self.plan,self.token,processes=self.proc,health_timeout=.001,grace=0,**kw)
    def healthy(self,pid,args):
        ui._write(self.stage/'ready.json',{'token':self.token,'child':self.proc.identity(pid)},exclusive=True)
    def test_real_verifier_unconfigured_key_blocks_installation(self):
        import base64
        raw=json.dumps({'schema':1,'repository':ui.updates.REPOSITORY,'version':45,
                        'filename':self.m.filename,'size':self.m.size,'sha256':self.m.sha256,
                        'signature':base64.b64encode(b'x'*64).decode('ascii')}).encode()
        self.verify.side_effect=REAL_VERIFY
        with patch.object(ui.updates,'PINNED_PUBLIC_KEY',None):
            with self.assertRaisesRegex(UpdateError,'approved pinned'):
                ui.prepare_update(self.target,raw,self.source,processes=self.proc)
        self.assertFalse((self.root/ui.CLAIM).exists())
    def test_unconfigured_key_fails_closed(self):
        self.verify.side_effect=UpdateError('No approved pinned signing key')
        with self.assertRaises(UpdateError): self.prepare()
        self.assertFalse((self.root/ui.CLAIM).exists())
    def test_relative_and_ads_rejected(self):
        with self.assertRaises(UpdateError): ui.safe_path(Path('relative.exe'))
        with self.assertRaises(UpdateError): ui.safe_path(self.root/'a:evil.exe',exists=False)
    def test_hardlink_rejected(self):
        import os
        linked=self.root/'linked.exe'; os.link(self.source,linked)
        with self.assertRaises(UpdateError): ui.safe_path(linked)
    def test_reparse_rejected(self):
        actual=self.target.lstat()
        class Info:
            st_mode=actual.st_mode; st_file_attributes=0x400; st_nlink=1
        with patch.object(Path,'lstat',return_value=Info()):
            with self.assertRaises(UpdateError): ui.safe_path(self.target)
    def test_candidate_wrong_hash(self):
        self.source.write_bytes(b'MZtampered')
        with self.assertRaises(UpdateError): self.prepare()
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_source_mode_rejected(self):
        self.packaged.side_effect=UpdateError('source mode')
        with self.assertRaises(UpdateError): self.prepare()
    def test_concurrent_claim_preserves_existing_stage(self):
        self.prepare(); previous=self.plan.read_bytes()
        with self.assertRaises(UpdateError): self.prepare()
        self.assertEqual(self.plan.read_bytes(),previous)
    def test_clean_cancellation(self):
        self.prepare(); ui.cancel_update(self.plan,self.token,processes=self.proc)
        self.assertFalse(self.stage.exists()); self.assertFalse((self.root/ui.CLAIM).exists())
    def test_unknown_artifact_blocks_cleanup(self):
        self.prepare(); unknown=self.stage/'valuable.txt'; unknown.write_text('keep')
        with self.assertRaises(UpdateError): ui.cancel_update(self.plan,self.token,processes=self.proc)
        self.assertTrue(unknown.exists())
    def test_live_helper_cancellation_blocked(self):
        self.prepare(); self.helper()
        ui._write(self.stage/'helper.json',{'token':self.token,'helper':self.proc.current()},exclusive=True)
        with self.assertRaises(UpdateError): ui.cancel_update(self.plan,self.token,processes=self.proc)
        self.assertTrue(self.plan.exists())
    def test_access_denied_is_not_assumed_dead_for_cleanup(self):
        self.prepare(); self.helper()
        ui._write(self.stage/'helper.json',{'token':self.token,'helper':self.proc.current()},exclusive=True)
        self.proc.open=lambda pid: (_ for _ in ()).throw(UpdateError('access denied'))
        with self.assertRaises(UpdateError): ui.cancel_update(self.plan,self.token,processes=self.proc)
        self.assertTrue(self.stage.exists())
    def test_launch_reservation_blocks_cleanup(self):
        self.prepare(); ui._write(self.stage/'launch.json',{'token':self.token,'helper':None},exclusive=True)
        with self.assertRaises(UpdateError): ui.cancel_update(self.plan,self.token,processes=self.proc)
    def test_tampered_plan_external_target(self):
        self.prepare(); self.data['target']=str(self.root.parent/'external.exe'); ui._write(self.plan,self.data)
        self.assertEqual(self.apply(),2)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_wrong_parent_pid_reuse(self):
        self.prepare(); self.proc.identities[11]['created']=101
        self.assertEqual(self.apply(),2)
        self.assertFalse((self.stage/'armed.json').exists())
    def test_wrong_parent_image(self):
        self.prepare(); self.proc.identities[11]['path']=str(self.source)
        self.assertEqual(self.apply(),2)
    def test_busy_parent_timeout(self):
        self.prepare(); self.proc.busy=True
        self.assertEqual(self.apply(parent_timeout=0),2)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
        self.assertTrue((self.stage/'armed.json').exists())
    def test_helper_reverifies_signature(self):
        self.prepare(); self.verify.side_effect=UpdateError('bad signature')
        self.assertEqual(self.apply(),2)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_downgrade_helper_version_mismatch(self):
        self.prepare(); self.data['current_version']=43; ui._write(self.plan,self.data)
        self.assertEqual(self.apply(),2)
    def test_tampered_candidate(self):
        self.prepare(); (self.stage/'candidate.exe').write_bytes(b'MZother')
        self.assertEqual(self.apply(),2)
    def test_stale_readiness_rejected_before_rename(self):
        self.prepare(); ui._write(self.stage/'ready.json',{'token':self.token},exclusive=True)
        self.assertEqual(self.apply(),2)
        self.assertFalse((self.stage/'backup.exe').exists())
    def test_success_retains_backup(self):
        self.prepare(); self.proc.on_launch=self.healthy
        self.assertEqual(self.apply(),0)
        self.assertEqual(self.target.read_bytes(),b'MZsigned-new')
        self.assertEqual((self.stage/'backup.exe').read_bytes(),b'MZtrusted-old')
        self.assertEqual(ui._read(self.stage/'journal.json')['state'],'ready')
    def test_timeout_rolls_back_and_launches_visible_old(self):
        self.prepare(); self.assertEqual(self.apply(),1)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
        self.assertEqual(self.proc.killed,[50])
        self.assertEqual(self.proc.launched[-1][1],['--update-restart'])
        self.assertFalse(self.proc.launched[-1][2]['hidden'])
    def test_spoof_ack_rolls_back(self):
        self.prepare()
        def spoof(pid,args):
            if len(self.proc.launched)==1:
                ui._write(self.stage/'ready.json',{'token':self.token,'child':self.proc.parent},exclusive=True)
        self.proc.on_launch=spoof
        self.assertEqual(self.apply(),1)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_failed_rollback_does_not_delete_unknown_target(self):
        self.prepare()
        def corrupt(pid,args): self.target.write_bytes(b'MZunknown-other-program')
        self.proc.on_launch=corrupt
        self.assertEqual(self.apply(),2)
        self.assertEqual(self.target.read_bytes(),b'MZunknown-other-program')
        self.assertEqual((self.stage/'backup.exe').read_bytes(),b'MZtrusted-old')
    def test_crash_between_backup_rename_and_journal(self):
        self.prepare(); self.helper(); self.proc.live[11]=False
        self.target.replace(self.stage/'backup.exe')
        self.assertEqual(ui.recover_update(self.plan,self.token,processes=self.proc),'rolled_back')
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_crash_between_swap_and_journal(self):
        self.prepare(); self.helper(); self.proc.live[11]=False
        self.target.replace(self.stage/'backup.exe'); (self.stage/'candidate.exe').replace(self.target)
        ui._journal(self.stage,self.token,'backed_up')
        self.assertEqual(ui.recover_update(self.plan,self.token,processes=self.proc),'rolled_back')
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_start_helper_fixed_arguments_hidden(self):
        self.prepare(); child=ui.start_helper(self.plan,self.token,processes=self.proc)
        self.assertEqual(child,50)
        exe,args,kw=self.proc.launched[0]
        self.assertEqual(exe,self.stage/'helper.exe'); self.assertTrue(kw['hidden'])
        self.assertEqual(args,['--update-helper',str(self.plan),'--update-token',self.token])
    def test_handshake_bound_to_owned_handle(self):
        self.prepare(); helper=ui.start_helper(self.plan,self.token,processes=self.proc)
        ui._write(self.stage/'armed.json',{'token':self.token,'parent':self.data['parent'],'helper':self.proc.identity(helper)},exclusive=True)
        self.assertTrue(ui.wait_for_arm(self.plan,self.token,processes=self.proc,helper=helper,timeout=.1))
    def test_handshake_rejects_reused_pid(self):
        self.prepare(); helper=ui.start_helper(self.plan,self.token,processes=self.proc)
        identity=self.proc.identity(helper); identity['created']+=1
        ui._write(self.stage/'armed.json',{'token':self.token,'parent':self.data['parent'],'helper':identity},exclusive=True)
        with self.assertRaises(UpdateError): ui.wait_for_arm(self.plan,self.token,processes=self.proc,helper=helper,timeout=.1)
    def test_old_onefile_bootloader_must_exit_before_rename(self):
        boot={'pid':12,'created':90,'path':str(self.target)}
        self.proc.bootloader=lambda parent: boot.copy()
        self.proc.identities[12]=boot; self.proc.live[12]=True
        self.prepare()
        self.assertEqual(self.apply(parent_timeout=0),2)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
        self.assertFalse((self.stage/'backup.exe').exists())
    def test_new_onefile_worker_ack_and_grace_use_worker_handle(self):
        self.prepare()
        def onefile(pid,args):
            worker={'pid':pid+100,'created':pid*100+1,'path':str(self.target)}
            self.proc.identities[worker['pid']]=worker; self.proc.live[worker['pid']]=True
            ui._write(self.stage/'ready.json',{'token':self.token,'child':worker},exclusive=True)
        def bind(p,identity):
            if identity['pid']!=p+100 or self.proc.identity(identity['pid'])!=identity:
                raise UpdateError('bad onefile worker')
            return identity['pid']
        self.proc.on_launch=onefile; self.proc.bind_startup=bind
        self.assertEqual(self.apply(),0)
        self.assertEqual(ui._read(self.stage/'child.json')['child']['pid'],150)
    def test_healthy_backup_cleanup_requires_explicit_approval(self):
        self.prepare(); self.proc.on_launch=self.healthy
        self.assertEqual(self.apply(),0)
        self.proc.live[22]=False
        with self.assertRaises(UpdateError): ui.cancel_update(self.plan,self.token,processes=self.proc)
        self.assertTrue((self.stage/'backup.exe').exists())
        ui.cancel_update(self.plan,self.token,processes=self.proc,discard_healthy_backup=True)
        self.assertFalse(self.stage.exists()); self.assertFalse((self.root/ui.CLAIM).exists())
    def test_non_mz_is_not_an_executable(self):
        self.source.write_bytes(b'not-a-pe')
        self.m=Manifest(45,'SmartTapo-V45.exe',8,ui.digest(self.source),b'x'*64,b'{}')
        self.verify.return_value=self.m
        with self.assertRaises(UpdateError): self.prepare()
    def test_config_preserved_without_reading_contents(self):
        config=self.root/'private.json'; config.write_text('not parsed by installer')
        self.plan=ui.prepare_update(self.target,b'{}',self.source,config_path=config,processes=self.proc)
        self.data=ui._read(self.plan); self.token=self.data['token']; self.stage=self.plan.parent
        self.assertEqual(self.apply(),1)
        self.assertEqual(self.proc.launched[-1][1],['--update-restart','--config',str(config)])
        self.assertEqual(config.read_text(),'not parsed by installer')
    def test_signed_manifest_size_mismatch(self):
        self.prepare(); self.verify.return_value=Manifest(45,'SmartTapo-V45.exe',1,self.m.sha256,b'x'*64,b'{}')
        self.assertEqual(self.apply(),2)
    def test_fails_if_grace_period_child_exits(self):
        self.prepare()
        def ready_then_exit(pid,args):
            if len(self.proc.launched)==1:
                self.healthy(pid,args); self.proc.live[pid]=False
        self.proc.on_launch=ready_then_exit
        self.assertEqual(self.apply(),1)
        self.assertEqual(self.target.read_bytes(),b'MZtrusted-old')
    def test_recovery_refuses_live_new_child(self):
        self.prepare(); self.helper(); self.proc.live[11]=False
        self.target.replace(self.stage/'backup.exe'); (self.stage/'candidate.exe').replace(self.target)
        child=self.proc.launch(self.target,[])
        ui._write(self.stage/'child.json',{'token':self.token,'child':self.proc.identity(child)},exclusive=True)
        with self.assertRaises(UpdateError): ui.recover_update(self.plan,self.token,processes=self.proc)
        self.assertEqual(self.target.read_bytes(),b'MZsigned-new')
    def test_recovery_discards_valid_interrupted_journal_temp(self):
        self.prepare(); self.helper(); self.proc.live[11]=False
        self.target.replace(self.stage/'backup.exe')
        ui._write(self.stage/'journal.json.tmp',{'token':self.token,'state':'backed_up'},exclusive=True)
        self.assertEqual(ui.recover_update(self.plan,self.token,processes=self.proc),'rolled_back')
        self.assertFalse((self.stage/'journal.json.tmp').exists())
    def test_recovery_refuses_unknown_target(self):
        self.prepare(); self.helper(); self.proc.live[11]=False
        self.target.replace(self.stage/'backup.exe'); self.target.write_bytes(b'MZforeign')
        with self.assertRaises(UpdateError): ui.recover_update(self.plan,self.token,processes=self.proc)
        self.assertEqual(self.target.read_bytes(),b'MZforeign')
    def test_real_gui_ack_requires_swapped_and_exact_image(self):
        self.prepare()
        with self.assertRaises(UpdateError): ui.acknowledge_startup(self.plan,self.token,processes=self.proc)
        self.target.write_bytes(b'MZsigned-new'); ui._journal(self.stage,self.token,'swapped')
        with patch.object(ui,'current_version',return_value=45):
            ui.acknowledge_startup(self.plan,self.token,processes=self.proc)
        self.assertEqual(ui._read(self.stage/'ready.json')['child'],self.proc.parent)

    def test_ack_rejects_signed_target_different_from_running_build(self):
        self.prepare(); self.target.write_bytes(b'MZsigned-new')
        ui._journal(self.stage,self.token,'swapped')
        self.verify.return_value=Manifest(46,self.m.filename,self.m.size,self.m.sha256,b'x'*64,b'{}')
        self.data['version']=46; ui._write(self.plan,self.data)
        with patch.object(ui,'current_version',return_value=45):
            with self.assertRaisesRegex(UpdateError,'running build'):
                ui.acknowledge_startup(self.plan,self.token,processes=self.proc)
        self.assertFalse((self.stage/'ready.json').exists())

    def test_three_consecutive_updates_retain_archived_backups_cleanup_spares_active_claim(self):
        manifests={}; completed=[]
        self.verify.side_effect=lambda raw,*a: manifests[raw]
        for version in (45,46,47):
            self.proc=Processes(self.target); self.packaged.return_value=self.target
            raw=str(version).encode(); self.source.write_bytes(b'MZnew-'+raw)
            manifests[raw]=Manifest(version,'SmartTapo-V%d.exe'%version,self.source.stat().st_size,ui.digest(self.source),b'x'*64,raw)
            with patch.object(ui,'current_version',return_value=version-1):
                self.plan=ui.prepare_update(self.target,raw,self.source,processes=self.proc)
                self.data=ui._read(self.plan); self.token=self.data['token']; self.stage=self.plan.parent
                self.proc.on_launch=self.healthy
                self.assertEqual(self.apply(),0)
            completed.append((self.plan,self.token,self.proc))
        for plan,token,proc in completed[:-1]:
            self.assertTrue((plan.parent/'completed.json').exists())
            self.assertTrue((plan.parent/'backup.exe').exists())
            proc.live[22]=False
            before=(self.root/ui.CLAIM).read_bytes()
            ui.cancel_update(plan,token,processes=proc,discard_healthy_backup=True)
            self.assertEqual((self.root/ui.CLAIM).read_bytes(),before)
        self.assertTrue(completed[-1][0].exists())

    def test_terminal_claim_not_released_on_wrong_version_tamper_or_pending_write(self):
        self.prepare(); self.proc.on_launch=self.healthy; self.assertEqual(self.apply(),0)
        claim=(self.root/ui.CLAIM).read_bytes()
        with self.assertRaises(UpdateError): ui._archive_completed_claim(self.target,44)
        with patch.object(ui,'current_version',return_value=45):
            ui._write(self.stage/'journal.json.tmp',{'token':self.token,'state':'ready'},exclusive=True)
            with self.assertRaises(UpdateError): ui._archive_completed_claim(self.target,45)
            (self.stage/'journal.json.tmp').unlink()
            self.verify.side_effect=UpdateError('signature invalid')
            with self.assertRaises(UpdateError): ui._archive_completed_claim(self.target,45)
        self.assertEqual((self.root/ui.CLAIM).read_bytes(),claim)

    def test_claim_archive_crash_before_and_after_atomic_rename(self):
        self.prepare(); self.proc.on_launch=self.healthy; self.assertEqual(self.apply(),0)
        with patch.object(ui,'current_version',return_value=45):
            with patch.object(ui.os,'rename',side_effect=OSError('simulated crash')):
                with self.assertRaises(OSError): ui._archive_completed_claim(self.target,45)
            self.assertTrue((self.root/ui.CLAIM).exists())
            ui._archive_completed_claim(self.target,45)
        self.assertFalse((self.root/ui.CLAIM).exists())
        self.assertTrue((self.stage/'completed.json').exists())
        self.assertTrue((self.stage/'backup.exe').exists())


if __name__=='__main__': unittest.main()
