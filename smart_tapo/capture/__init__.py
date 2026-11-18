"""Safe emulator capture; clock-pixel changes are NOT OCR or proof of live video."""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from PIL import Image

from smart_tapo.capture.live_verifier import LiveVerifier, LiveVerificationError
from smart_tapo.capture.page_mode import PageMode,classify_page,foreground_activity,inactivity_nodes
from smart_tapo.capture.ui_lock import ui_lock
from smart_tapo.capture.coordination import navigation_scope, navigation_snapshot, navigation_lock
from dataclasses import replace
import threading
from contextlib import contextmanager
from smart_tapo.capture.ui_diagnostics import observation
from smart_tapo.frames import image_storage_bytes, publish_frame
from smart_tapo.runtime import hidden_console_options

LOG = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    pass


class DiagnosticCaptureError(CaptureError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('UI collection failed: ' + reason)


class LiveRecoveryError(CaptureError):
    """Fixed reason codes survive credential-free operator/audit translation."""
    reason='selected_camera_authorization_missing'


class Adb:
    """argv-only ADB adapter; never expose subprocess output containing UI secrets."""

    def __init__(self, config, runner=subprocess.run, launcher=subprocess.Popen):
        if not config.adb_serial or config.adb_serial.startswith('-'):
            raise ValueError('Configure an explicit adb_serial')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', config.tapo_package):
            raise ValueError('tapo_package must be an Android package identifier')
        self.config, self.runner, self.launcher = config, runner, launcher
        self.started = False
        self.last_ui_request_id = None
        self._epoch = uuid.uuid4().hex
        self._live_authorization = None
        self._last_page_reason = None
        self._ui_diagnostic = threading.local()

    def begin_session(self):
        self._epoch=uuid.uuid4().hex
        self._last_page_reason=None
        self._clear_authorization()

    def _clear_authorization(self):
        self._live_authorization = None

    def _authorized(self):
        proof=self._live_authorization
        state=navigation_snapshot(self.config)
        return (proof is not None and 0 <= time.monotonic()-proof[0] < min(10,self.config.ui_check_interval_seconds)
                and proof[1:] == (repr(self.config),self._epoch,state.generation)
                and not state.recovering)

    def ui_check_diagnostics(self):
        detail = dict(getattr(self._ui_diagnostic, 'detail', observation()))
        if getattr(self._ui_diagnostic, 'failed_stage', None):
            detail['stage'] = self._ui_diagnostic.failed_stage
        return detail

    @contextmanager
    def _ui_phase(self, stage, field):
        detail = getattr(self._ui_diagnostic, 'detail', None)
        started = time.perf_counter()
        if detail is not None:
            detail['stage'] = stage
        try:
            yield
        except Exception:
            if detail is not None:
                self._ui_diagnostic.failed_stage = stage
            raise
        finally:
            if detail is not None:
                elapsed = max(0.0, time.perf_counter() - started)
                detail[field] = float((detail[field] or 0.0) + elapsed)

    @contextmanager
    def _diagnostic_lock(self):
        manager = ui_lock(self.config.adb_serial, min(self.config.adb_timeout_seconds, 5))
        try:
            with self._ui_phase('lock', 'lock_seconds'):
                manager.__enter__()
        except ValueError as exc:
            reason = 'ui_lock_unavailable' if exc.args == ('ui_dump_lock_unavailable',) else 'ui_lock_busy'
            raise DiagnosticCaptureError(reason) from None
        try:
            yield
        finally:
            manager.__exit__(None, None, None)

    def run(self, *args, selected=True):
        argv = [self.config.adb_path]
        if selected:
            argv += ['-s', self.config.adb_serial]
        argv += list(args)
        try:
            result = self.runner(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=self.config.adb_timeout_seconds, check=False,
                                 **hidden_console_options())
        except subprocess.TimeoutExpired:
            self._clear_authorization()
            raise DiagnosticCaptureError('adb_timeout') from None
        except OSError:
            self._clear_authorization()
            raise DiagnosticCaptureError('adb_unavailable') from None
        if result.returncode:
            self._clear_authorization()
            raise DiagnosticCaptureError('adb_nonzero')
        return result.stdout

    def connect(self, stop):
        try:return self._connect(stop)
        except Exception:
            self._clear_authorization()
            raise

    def _connect(self, stop):
        if stop.is_set():
            self._clear_authorization()
            raise CaptureError('Capture stopped')
        # Capture retries connect after a negative read-only verification. Preserve
        # only a recent recognized modal on a still-continuous selected transport.
        if self._last_page_reason != 'live_inactivity_confirmation' or not self._authorized():
            self._clear_authorization()
        if self._live_authorization is not None:
            try:
                if self.run('get-state').strip()!=b'device':self._clear_authorization()
            except CaptureError:self._clear_authorization()
        serial = self.config.adb_serial
        if ':' in serial:
            if not re.fullmatch(r'(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}', serial):
                raise CaptureError('TCP adb_serial must use configured loopback host:port')
            if not 1 <= int(serial.rsplit(':', 1)[1]) <= 65535:
                raise CaptureError('Invalid ADB TCP port')
            try:
                self.run('connect', serial, selected=False)
            except CaptureError:
                pass  # A stopped emulator may not yet expose its configured port.
        try:
            available = self.run('get-state').strip() == b'device'
        except CaptureError:
            available = False
        if not available:self._clear_authorization()
        if not available and self.config.memu_executable and not self.started:
            if os.name != 'nt':
                raise CaptureError('Automatic MEmu startup is supported only on Windows')
            self.started = True
            try:
                self.launcher([self.config.memu_executable], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
            except OSError:
                raise CaptureError('Cannot start configured MEmu executable') from None
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                if ':' in serial:
                    self.run('connect', serial, selected=False)
                if self.run('shell', 'getprop', 'sys.boot_completed').strip() == b'1':
                    return
            except CaptureError:
                pass
            stop.wait(min(1.0, max(0, deadline - time.monotonic())))
        self._clear_authorization()
        raise CaptureError('Emulator boot timed out or capture stopped')

    def ui(self):
        self.last_ui_request_id = None
        try:
            with self._diagnostic_lock():
                return self._fresh_ui()
        except ValueError:
            self._clear_authorization()
            raise CaptureError('Fresh UI collection is busy or unavailable') from None
        except Exception:
            self._clear_authorization()
            raise

    def _fresh_ui(self):
        identifier = uuid.uuid4().hex
        path = '/sdcard/smart-tapo-window-' + identifier + '.xml'
        try:
            with self._ui_phase('ui_dump', 'dump_seconds'):
                result = self.run('shell', 'uiautomator', 'dump', path)
            # Android may report dump errors with exit 0. Never read an older
            # shared filename, even when the process exit status looks good.
            if not isinstance(result, bytes) or len(result)>65536 or b'error' in result.lower():
                self._ui_diagnostic.failed_stage = 'ui_dump'
                raise DiagnosticCaptureError('ui_dump_failed')
            with self._ui_phase('ui_read', 'read_seconds'):
                xml = self.run('shell', 'cat', path)
            with self._ui_phase('classify', 'parse_seconds'):
                if not isinstance(xml,bytes) or not xml or len(xml)>1024*1024:
                    raise DiagnosticCaptureError('ui_hierarchy_invalid')
                try:
                    if ET.fromstring(xml).tag!='hierarchy':raise ValueError
                except (ET.ParseError,ValueError):
                    raise DiagnosticCaptureError('ui_hierarchy_invalid') from None
        finally:
            # Unique argv-only owned path: concurrent reads cannot remove each
            # other's file. A cleanup error also fails closed, never trusts XML.
            with self._ui_phase('ui_cleanup', 'cleanup_seconds'):
                self.run('shell', 'rm', '-f', path)
        self.last_ui_request_id = identifier
        return xml

    def ensure_live(self, stop=None):
        try:return self._ensure_live(stop)
        except Exception:
            self._clear_authorization()
            raise

    def _ensure_live(self, stop=None):
        # Recovery already checks healthy Live without navigating. Do not spend
        # the existing 10s authorization budget on a redundant preflight dump.
        return self.recover_live(stop)

    def _settle_fresh_live(self, mode):
        """Caller holds UI lock and has just obtained fresh selected Live proof."""
        state=navigation_snapshot(self.config)
        # A killed mutation owner or a prior boot's monotonic cutoff cannot
        # strand a new explicit run forever. Never clear based on stored XML.
        if state.recovering or state.changed_monotonic>time.monotonic():
            with navigation_scope(self.config):pass
            state=navigation_snapshot(self.config)
        return replace(mode,navigation_generation=state.generation)

    def _read_page_evidence(self):
        started=time.monotonic()
        try:
            state=navigation_snapshot(self.config)
            with self._ui_phase('foreground_before', 'foreground_seconds'):
                before=foreground_activity(self.run('shell','dumpsys','activity','activities'),self.config.tapo_package)
            xml=self.ui()
            with self._ui_phase('foreground_after', 'foreground_seconds'):
                after=foreground_activity(self.run('shell','dumpsys','activity','activities'),self.config.tapo_package)
            with self._ui_phase('classify', 'parse_seconds'):
                mode=(PageMode('unknown','foreground_changed_or_unknown') if before is None or before!=after
                      else classify_page(xml,self.config.camera_name,after,self.config.tapo_package))
            current=navigation_snapshot(self.config)
            if current!=state or current.recovering:
                self._clear_authorization()
            elif mode.mode=='live':
                self._live_authorization=(started,repr(self.config),self._epoch,current.generation)
            elif mode.reason!='live_inactivity_confirmation' or not self._authorized():
                self._clear_authorization()
            self._last_page_reason=mode.reason
            return mode,xml
        except Exception:
            self._clear_authorization()
            raise

    def read_page_mode(self):
        """Read-only foreground/XML/foreground collected under one shared lock."""
        self._ui_diagnostic.detail = observation()
        self._ui_diagnostic.failed_stage = None
        try:
            with self._diagnostic_lock():
                mode, _ = self._read_page_evidence()
                state = navigation_snapshot(self.config)
                if state.recovering: return PageMode('unknown','navigation_in_progress')
                return replace(mode, navigation_generation=state.generation)
        except ValueError:
            self._clear_authorization()
            raise CaptureError('Invalid page diagnostics or navigation state') from None
        except Exception:
            self._clear_authorization()
            raise

    def recover_live(self, stop=None):
        try:return self._recover_live(stop)
        except Exception:
            self._clear_authorization()
            raise

    def _recover_live(self, stop=None):
        """Bounded observed-overlay repair; never infer success from a tap."""
        stop = stop or threading.Event()
        try:
            for attempt in range(3):
                if stop.is_set():
                    self._clear_authorization()
                    raise CaptureError('Live recovery stopped')
                # Acquire both locks BEFORE collecting actionable UI. This is
                # fresh after waiting, without a second slow dump after the
                # decision-lock wait consuming the remaining authorization.
                with ui_lock(self.config.adb_serial, min(self.config.adb_timeout_seconds,5)), \
                        navigation_lock(self.config.adb_serial,min(self.config.adb_timeout_seconds,5)):
                    if stop.is_set():raise CaptureError('Live recovery stopped')
                    fence=(repr(self.config),self._epoch,navigation_snapshot(self.config))
                    collected=time.monotonic()
                    mode, xml = self._read_page_evidence()
                    if fence!=(repr(self.config),self._epoch,navigation_snapshot(self.config)):
                        raise CaptureError('Recovery evidence crossed a navigation boundary')
                    if mode.mode == 'live':
                        return self._settle_fresh_live(mode)
                    if fence[2].recovering or fence[2].changed_monotonic>collected:
                        raise CaptureError('Navigation boundary requires fresh Live proof')
                    if not 0<=time.monotonic()-collected<min(10,self.config.ui_check_interval_seconds):
                        raise CaptureError('Recovery UI evidence expired')
                    if mode.reason == 'live_inactivity_confirmation':
                        continued=self._continue_inactivity(xml,stop)
                        if continued.mode=='live':return continued
                        # Continue can reveal a second, selected-camera refresh
                        # overlay. It is NOT Live and must be freshly verified
                        # by the next bounded step before another tap.
                        if continued.reason=='live_refresh_required':continue
                        raise CaptureError('Inactivity continuation is not Live')
                    if mode.reason != 'live_refresh_required':
                        raise CaptureError('No safely recognized Live refresh overlay')
                    point = refresh_center(xml, self.config)
                    if point is None: raise CaptureError('Refresh overlay has no unique safe accessibility bounds')
                    if stop.is_set(): raise CaptureError('Live recovery stopped')
                    with navigation_scope(self.config):
                        if stop.is_set():raise CaptureError('Live recovery stopped')
                        self.run('shell','input','tap',str(point[0]),str(point[1]))
                if stop.wait(0.5 * (2 ** attempt)):
                    raise CaptureError('Live recovery stopped')
            mode = self.read_page_mode()
            if mode.mode == 'live': return mode
        except ValueError:
            raise CaptureError('Live recovery coordination unavailable') from None
        raise CaptureError('Live recovery exhausted; reopen camera manually')

    def _continue_inactivity(self,xml,stop):
        """Caller collected XML under UI+decision locks, still holds both."""
        try:
            point=inactivity_center(xml,self.config)
            if point is None:raise CaptureError('Invalid inactivity continuation target')
            if stop.is_set():raise CaptureError('Live recovery stopped')
            # Explicit operator permission covers ONLY this freshly recognized
            # inactivity Continue action, not Live, refresh, or arbitrary UI.
            # Selected-camera proof must be obtained anew after dismissal.
            self._clear_authorization()
            with navigation_scope(self.config):
                if stop.is_set():raise CaptureError('Live recovery stopped')
                self.run('shell','input','tap',str(point[0]),str(point[1]))
            for delay in (.25,.5,1):
                if stop.wait(delay):raise CaptureError('Live recovery stopped')
                mode,_=self._read_page_evidence()
                if mode.mode=='live':return self._settle_fresh_live(mode)
                if mode.reason=='live_refresh_required':return mode
                if mode.reason!='live_inactivity_confirmation':break
            raise CaptureError('Inactivity continuation did not restore selected Live')
        except Exception:
            self._clear_authorization()
            raise

    def verify_live(self):
        return self.read_page_mode()


def nodes(xml):
    try:
        return list(ET.fromstring(xml).iter('node'))
    except ET.ParseError:
        raise CaptureError('Invalid UI hierarchy; navigate to live view manually') from None


def labels(xml):
    return {n.get(k, '').strip() for n in nodes(xml) for k in ('text', 'content-desc')}


def live_ui(xml, camera_name, *, activity=None):
    return classify_page(xml,camera_name,activity).mode=='live'


def inactivity_center(xml,config):
    matched=inactivity_nodes(nodes(xml))
    if matched is None:return None
    node=matched['continue_btn']
    if node.get('enabled')!='true' or node.get('clickable')!='true' or node.get('visible-to-user')=='false':return None
    bounds=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',node.get('bounds',''))
    if bounds is None:return None
    left,top,right,bottom=map(int,bounds.groups())
    if not (0<=left<right<=config.expected_size[0] and 0<=top<bottom<=config.expected_size[1]):return None
    return ((left+right)//2,(top+bottom)//2)


def refresh_center(xml, config):
    from .page_mode import REFRESH_LABELS
    matches = [node for node in nodes(xml) if any(
        node.get(key, '').strip().casefold() in REFRESH_LABELS
        for key in ('text', 'content-desc'))]
    if len(matches) != 1: return None
    node = matches[0]
    if node.get('enabled') == 'false' or node.get('visible-to-user') == 'false': return None
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds',''))
    if not match: return None
    left, top, right, bottom = map(int, match.groups())
    vl, vt, vr, vb = config.video.box()
    if not (0 <= vl <= left < right <= vr <= config.expected_size[0]
            and 0 <= vt <= top < bottom <= vb <= config.expected_size[1]): return None
    return ((left+right)//2, (top+bottom)//2)


def camera_center(xml, camera_name, size):
    tree = nodes(xml)
    text = labels(xml)
    if not text.intersection({'Home', 'หน้าหลัก', 'บ้าน', 'อุปกรณ์', 'Devices'}):
        return None
    # Never tap through a login/password screen, even if it contains the camera name.
    if any(n.get('password') == 'true' for n in tree):
        return None
    matches = [n for n in tree if camera_name in
               (n.get('text', '').strip(), n.get('content-desc', '').strip())]
    if len(matches) != 1:
        return None
    match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', matches[0].get('bounds', ''))
    if not match:
        return None
    x, y, right, bottom = map(int, match.groups())
    if not (0 <= x < right <= size[0] and 0 <= y < bottom <= size[1]):
        return None
    return ((x + right) // 2, (y + bottom) // 2)


def decode_clock(png, config):
    """Pixel-difference freshness heuristic, NOT OCR or proof of live video."""
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != 'PNG' or image.size != config.expected_size:
                raise CaptureError('Screenshot must be PNG with configured dimensions')
            image.load()
            return image.convert('RGB').crop(config.clock_roi.box()).tobytes()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise CaptureError('Screenshot PNG decode failed') from None


def capture(config, stop, once=False, adb=None, monotonic=time.monotonic):
    config.validate()
    if config.capture_backend == 'rtsp' and adb is None:
        from .rtsp import capture_rtsp
        return capture_rtsp(config, stop, once=once)
    adb = adb or Adb(config)
    if isinstance(adb,Adb):adb.begin_session()
    diagnostic_options = {'diagnostics_getter': adb.ui_check_diagnostics} if isinstance(adb, Adb) else {}
    verifier = LiveVerifier(adb.verify_live,
                            config.ui_check_interval_seconds, **diagnostic_options)
    try:
        return _capture(config, stop, once, adb, monotonic, verifier)
    finally:
        verifier.close()
        if isinstance(adb,Adb):adb._clear_authorization()


def _capture(config, stop, once, adb, monotonic, verifier):
    from .clock_window import PendingWindow
    window = PendingWindow(config) if getattr(config, 'clock_window_enabled', False) else None
    try:
        return _capture_loop(config, stop, once, adb, monotonic, verifier, window)
    finally:
        if window is not None: window.reset()


def _capture_loop(config, stop, once, adb, monotonic, verifier, window):
    session = uuid.uuid4().hex
    sequence, previous, connected = 0, None, False
    last_diagnostic = float('-inf')
    last_ui_warning = float('-inf')
    ui_paused = False
    target = monotonic()
    while not stop.is_set():
        if stop.wait(max(0, target - monotonic())):
            break
        now = monotonic()
        cycle_started = time.perf_counter()
        missed = max(0, int((now - target) // config.capture_interval_seconds))
        sequence += missed + 1
        target += (missed + 1) * config.capture_interval_seconds
        try:
            if not connected:
                adb.connect(stop)
                with ui_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)):
                    if isinstance(adb, Adb): adb.ensure_live(stop)
                    else: adb.ensure_live()
                    established_generation = navigation_snapshot(config).generation
                connected = True
                verifier.establish(monotonic())
            verifier.check(monotonic())
            navigation_before = navigation_snapshot(config)
            if navigation_before.recovering or navigation_before.generation != established_generation:
                raise CaptureError('Navigation boundary requires fresh Live proof')
            png = adb.run('exec-out', 'screencap', '-p')
            captured_at, captured_monotonic = time.time(), monotonic()
            clock = decode_clock(png, config)
            # Consume results after capture; never publish with expired proof.
            if not verifier.check(monotonic()):
                previous = None
                if window is not None: window.reset('ui_proof_unavailable')
                ui_paused = True
                if captured_monotonic-last_ui_warning >= 5:
                    LOG.warning('Live UI verification pending: capture publication paused')
                    last_ui_warning = captured_monotonic
                if once:
                    return 1
                continue
            if ui_paused:
                LOG.info('Live UI verification restored')
                ui_paused = False
            fresh = window is None and previous is not None and clock != previous
            if window is None: previous = clock
            if image_storage_bytes(config) + len(png) > config.max_image_storage_mb * 1_000_000:
                if window is not None: window.reset('image_quota')
                previous = None
                LOG.warning('Image quota full; publishing paused; tentative window discarded')
                if window is not None and once: return 1
            else:
                # Publication is atomic with navigation, not slow read-only UI dumps.
                with navigation_lock(config.adb_serial, min(config.adb_timeout_seconds, 5)):
                    navigation_after = navigation_snapshot(config)
                    if (navigation_after.recovering or navigation_before != navigation_after
                            or captured_monotonic < navigation_after.changed_monotonic):
                        raise CaptureError('Frame crossed navigation boundary')
                    # Lock contention may outlive proof or complete a negative
                    # asynchronous check without changing navigation generation.
                    publication_now = monotonic()
                    if not verifier.check(publication_now):
                        previous = None
                        if window is not None: window.reset('ui_proof_unavailable')
                        ui_paused = True
                        if publication_now-last_ui_warning >= 5:
                            LOG.warning('Live UI verification pending: capture publication paused')
                            last_ui_warning = publication_now
                        if once:
                            return 1
                        continue
                    if stop.is_set():
                        if window is not None: window.reset('capture_uncertainty')
                        break
                    if window is not None:
                        rows, proof = window.add(png, clock, captured_at, captured_monotonic,
                                                 sequence, session, navigation_after.generation)
                        if not rows:
                            if once: return 1  # No confirmed ready artifact exists yet.
                            continue
                        # Carry the independently bounded selected-Live UI proof.
                        ui_at = getattr(verifier, 'verified_at', publication_now)
                        proof.update(ui_at=ui_at, ui_until=ui_at+config.ui_check_interval_seconds)
                        # Validate the whole owned batch BEFORE making any marker ready.
                        from .clock_window import valid_window
                        from smart_tapo.frames import Frame
                        sample = Frame('pending', config.camera_id, session, rows[0][1],
                            rows[0][2], rows[0][3], *config.expected_size, 'pending.png', True,
                            window_proof=proof)
                        if (time.time()-rows[0][2] > config.clock_check_interval_seconds
                                or monotonic()-rows[0][3] > config.clock_check_interval_seconds
                                or not valid_window(sample, config, navigation_after, time.time(), monotonic())
                                or image_storage_bytes(config)+sum(p.stat().st_size for p, *_ in rows)
                                    > config.max_image_storage_mb*1_000_000
                                or any(p.is_symlink() or not p.is_file() for p, *_ in rows)):
                            window.reset('publication_validation_failed')
                            if once: return 1
                            continue
                        for path, seq, at, mono in rows:
                            if (stop.is_set() or not verifier.check(monotonic())
                                    or time.time()-at > config.clock_check_interval_seconds
                                    or monotonic()-mono > config.clock_check_interval_seconds):
                                raise CaptureError('Window publication interrupted')
                            marker = publish_frame(config, path.read_bytes(), session, seq, True,
                                captured_at=at, captured_monotonic=mono, window_proof=proof)
                        window.reset()
                        # End sample becomes the next window's clock baseline, not a duplicate image.
                        window.add(png, clock, captured_at, captured_monotonic, sequence,
                                   session, navigation_after.generation)
                        fresh = True
                    else:
                        marker = publish_frame(config, png, session, sequence, fresh,
                                               captured_at=captured_at, captured_monotonic=captured_monotonic)
                elapsed_seconds = max(0, time.perf_counter()-cycle_started)
                if (captured_monotonic - last_diagnostic >= 5 or missed
                        or elapsed_seconds > config.capture_interval_seconds):
                    LOG.info('Capture sample', extra={'frame_context': {
                        'frame_id': marker.name[:-len('.ready.json')], 'session_id': session,
                        'sequence': sequence, 'captured_at': captured_at,
                        'captured_monotonic': captured_monotonic, 'fresh': fresh,
                        'elapsed_seconds': elapsed_seconds, 'missed_slots': missed}})
                    last_diagnostic = captured_monotonic
        except (CaptureError, OSError, ValueError) as error:
            LOG.warning('Capture unavailable: %s', error)
            repairable=(connected and isinstance(adb,Adb)
                        and isinstance(error,LiveVerificationError)
                        and error.reason in ('live_refresh_required','live_inactivity_confirmation'))
            if window is not None: window.reset('capture_uncertainty')
            connected, previous = False, None
            session, sequence = uuid.uuid4().hex, 0
            verifier.reset()
            if repairable and not stop.is_set():
                LOG.info('Live overlay recovery started')
                # A fresh successful UI read identified a known overlay, not a
                # lost transport. Reconnecting here wastes the short-lived
                # selected-camera authorization and can strand every later
                # attempt behind the same sheet. Repair before reconnecting;
                # recover_live still enforces fresh UI, TTL, Stop and fences.
                try:
                    with ui_lock(config.adb_serial,min(config.adb_timeout_seconds,5)):
                        restored=adb.recover_live(stop)
                        if not isinstance(restored,PageMode) or restored.mode!='live':
                            raise CaptureError('Overlay recovery did not verify Live')
                        established_generation=navigation_snapshot(config).generation
                    verifier.establish(monotonic())
                    connected=True
                    LOG.info('Live overlay recovery restored')
                except (CaptureError,OSError,ValueError) as recovery_error:
                    LOG.warning('Capture unavailable: %s',recovery_error)
            if once:
                return 1
            if connected:
                continue
            stop.wait(min(1.0, config.capture_interval_seconds))
        if once:
            return 0
    return 0
