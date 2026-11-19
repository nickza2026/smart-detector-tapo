"""Explicit offline import of the shipped, reviewed recording kit; no discovery."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import uuid

from .announcement_profile import (_identifier, _pairs, _pinned_file, _regular,
                                   load_announcement_profile)
from .profile_setup import _directory, _identity, _path, _pin, create_private_profile

_LIMIT = 32 * 1024
_NAMES = {'paid_ready': 'ready.wav', 'end': 'end.wav'}
BUNDLED_MANIFEST = Path(__file__).resolve().parent / 'assets' / 'prepared-profile' / 'profile.json'
_OLD_FV2_REFERENCE = '44b6c450642e9410206ccc76f45edc15d08416e89460160bcb08ebc98894ff99'
_VOICE_FV2_REFERENCE = '5543e07cfe9ff78e2223ce41367752f5a9f0c0325d439ba7b8dc1ca60ef3665c'
_ALTERNATE_FV2_REFERENCE = 'a127d02058c12a6cff7da3308c833691564aa7f0eb7e9101cb9318a7f9c1d7b6'
_LEGACY_FV1_REFERENCES = {
    2.: {'b44050daaefdc96d5219b76808308e1208fe2c61b5e89b24c5ab3c944cdf9a77',
         '068df15d84b82e9c527367482c91d53f259f2881508dfee9e1105d1832cab8fa'},
    4.: {'27c7e823cd65e6d30dc8327a216d3d431d12f084afeffa7a3933e3f0a6064af7',
         'd7ef14ea16d8866e6270248ab2bbabe1d59b2046c9e2c45b43aa67163f1922f5'}}
_KNOWN_OLD_REFERENCES = {_OLD_FV2_REFERENCE,_VOICE_FV2_REFERENCE}.union(
    *_LEGACY_FV1_REFERENCES.values())


def default_prepared_manifest(base):
    """Custom sidecars win; the exact old shipped kit cannot shadow its repair."""
    beside = Path(base) / 'prepared-profile' / 'profile.json'
    if beside.is_file():
        try:
            old = load_announcement_profile(beside, 'provided-recordings')
            if (old.fingerprint in _KNOWN_OLD_REFERENCES and
                    load_announcement_profile(BUNDLED_MANIFEST, 'provided-recordings').fingerprint
                    == _ALTERNATE_FV2_REFERENCE):
                return BUNDLED_MANIFEST
        except Exception:
            # Keep the selected sidecar so existing validation rejects it;
            # an unsafe/corrupt/custom input is not permission for a fallback.
            pass
    return beside if beside.is_file() else BUNDLED_MANIFEST


def _json(path):
    with _pinned_file(path, _LIMIT):
        return json.loads(path.read_bytes().decode('utf-8'), object_pairs_hook=_pairs)


def _canonical(data):
    entries = data['templates']
    if (len(entries) != 2 or {e['kind'] for e in entries} != set(_NAMES)
            or any(e['file'] != _NAMES[e['kind']] for e in entries)):
        raise ValueError
    return sorted(entries, key=lambda e: e['kind'])


def _cached(stack, profiles, receipt, camera, reference, templates, feature_version=1):
    """Only consult the constrained pointer, never enumerate private profiles."""
    if (type(receipt) is not dict or set(receipt) != {
            'schema_version', 'camera_id', 'reference_fingerprint', 'profile'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or not _identifier(receipt['camera_id'])
            or type(receipt['reference_fingerprint']) is not str
            or re.fullmatch('[0-9a-f]{64}', receipt['reference_fingerprint']) is None
            or type(receipt['profile']) is not str
            or re.fullmatch('private_[0-9a-f]{32}/profile.json', receipt['profile']) is None):
        raise ValueError
    target = profiles / receipt['profile']
    # A safely absent cache is obsolete; any existing unsafe ancestry fails closed.
    try:
        target.parent.lstat()
    except FileNotFoundError:
        return None
    _pin(stack, target.parent)
    for name in ('profile.json', 'ready.wav', 'end.wav'):
        file = target.parent / name
        try:
            file.lstat()
        except FileNotFoundError:
            continue
        stack.enter_context(_pinned_file(file, _LIMIT if name == 'profile.json' else 64 * 1024 * 1024))
    if receipt['camera_id'] != camera or receipt['reference_fingerprint'] != reference:
        return None
    try:
        installed = load_announcement_profile(target, camera)
        data = _json(target)
        if (installed.id == target.parent.name and _canonical(data) == templates
                and installed.feature_version == feature_version):
            return target
    except ValueError:
        pass  # Safely regular but obsolete/corrupt private content is not reused.
    return None


def needs_prepared_upgrade(data_dir, camera_id, selected_path, reference_manifest) -> bool:
    """Read-only preflight for an explicit consent flow; never discovers profiles.

    Only the pointed app-managed legacy version or exact old FV2 kit qualifies.
    Unsafe, missing, corrupt or custom selections return False, never a fallback.
    Callers must recheck selection/state before importing after consent.
    """
    try:
        if not _identifier(camera_id):
            return False
        root, selected, reference = _path(data_dir), _path(selected_path), _path(reference_manifest)
        profiles = root / 'announcement-profiles'
        if (reference.name != 'profile.json' or reference.parent.name != 'prepared-profile'
                or selected.parent.parent != profiles or selected.name != 'profile.json'
                or re.fullmatch('private_[0-9a-f]{32}', selected.parent.name) is None):
            return False
        with ExitStack() as stack:
            _pin(stack, profiles)
            _pin(stack, reference.parent)
            stack.enter_context(_pinned_file(reference, _LIMIT))
            kit = load_announcement_profile(reference, 'provided-recordings')
            if kit.feature_version != 2:
                return False
            templates = _canonical(_json(reference))
            pointer = profiles / 'prepared-selection.json'
            stack.enter_context(_pinned_file(pointer, _LIMIT))
            receipt = _json(pointer)
            if type(receipt) is not dict:
                return False
            # _cached validates the entire receipt before using its relative path.
            target = _cached(stack, profiles, receipt, camera_id,
                             receipt.get('reference_fingerprint'), templates, 1)
            if target is not None and target == selected:
                return True
            if kit.fingerprint not in (_VOICE_FV2_REFERENCE,_ALTERNATE_FV2_REFERENCE):
                return False
            for start,end,known in ((2.,6.,_OLD_FV2_REFERENCE),(4.,8.,_VOICE_FV2_REFERENCE)):
                legacy = [dict(entry) for entry in templates]
                ready = next(entry for entry in legacy if entry['kind'] == 'paid_ready')
                ready.pop('alternates',None)
                ready['start'],ready['end']=start,end
                for version in (1,2):
                    expected={known} if version==2 else _LEGACY_FV1_REFERENCES[start]
                    if receipt.get('reference_fingerprint') not in expected:
                        continue
                    target = _cached(stack, profiles, receipt, camera_id,
                                     receipt.get('reference_fingerprint'), legacy, version)
                    if target is not None and target == selected:
                        return True
            return False
    except Exception:
        return False


def prepare_profile(data_dir, camera_id, reference_manifest) -> Path:
    """Validate the current kit, then reuse or explicitly copy a camera-bound profile.

    The small owned receipt is the only cache index. No files are removed, except
    an exclusively created temporary receipt if its publication fails.
    """
    temporary = None
    temporary_id = None
    try:
        if not _identifier(camera_id):
            raise ValueError
        reference = _path(reference_manifest)
        if reference.name != 'profile.json' or reference.parent.name != 'prepared-profile':
            raise ValueError
        with ExitStack() as stack:
            _pin(stack, reference.parent)
            stack.enter_context(_pinned_file(reference, _LIMIT))
            for name in _NAMES.values():
                stack.enter_context(_pinned_file(reference.parent / name, 64 * 1024 * 1024))
            kit = load_announcement_profile(reference, 'provided-recordings')
            templates = _canonical(_json(reference))
            root = _path(data_dir)
            _directory(stack, root)
            profiles = root / 'announcement-profiles'
            _directory(stack, profiles)
            pointer = profiles / 'prepared-selection.json'
            previous = None
            try:
                pointer.lstat()
            except FileNotFoundError:
                pass
            else:
                with _pinned_file(pointer, _LIMIT):
                    previous = _identity(pointer.lstat())
                    receipt = _json(pointer)
                cached = _cached(stack, profiles, receipt, camera_id, kit.fingerprint, templates,
                                 kit.feature_version)
                if cached is not None:
                    return cached
            records = [{k: v for k, v in entry.items() if k != 'sha256'} for entry in templates]
            for entry in records:
                entry['file'] = reference.parent / entry['file']
            result = create_private_profile(root, camera_id, records, feature_version=kit.feature_version)
            # Verify the exact settings and bytes, not just a successful creation.
            installed = load_announcement_profile(result, camera_id)
            if installed.feature_version != kit.feature_version or _canonical(_json(result)) != templates:
                raise ValueError
            receipt = {'schema_version': 1, 'camera_id': camera_id,
                       'reference_fingerprint': kit.fingerprint,
                       'profile': result.parent.name + '/profile.json'}
            candidate = profiles / ('prepared-selection-' + uuid.uuid4().hex + '.tmp')
            with candidate.open('x', encoding='utf-8') as output:
                temporary, temporary_id = candidate, _identity(os.fstat(output.fileno()))
                json.dump(receipt, output, ensure_ascii=True, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            if previous is None:
                # Exclusive publication avoids replacing a concurrently introduced file.
                os.link(temporary, pointer)
                temporary.unlink()
            else:
                _regular(pointer, _LIMIT)
                if _identity(pointer.lstat()) != previous:
                    raise ValueError
                os.replace(temporary, pointer)
            temporary = None
            return result
    except Exception:
        raise ValueError('prepared_profile_unavailable') from None
    finally:
        if temporary is not None:
            try:
                with ExitStack() as cleanup:
                    _pin(cleanup, temporary.parent)
                    if _identity(_regular(temporary, _LIMIT)) == temporary_id:
                        temporary.unlink()
            except Exception:
                pass
