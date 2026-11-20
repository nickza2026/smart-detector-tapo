"""Offline operator CLI: explicit new encrypted keys and local manifests only.

No credential-store access, trust-root changes, uploads or publication.
"""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
# Direct script invocation must find the source helper, not an installed package.
sys.path.insert(0, str(ROOT))
from smart_tapo.desktop_storage import _dpapi
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPOSITORY = 'nickza2026/smart-detector-tapo'
HEADER = b'SMART-TAPO-DPAPI-ED25519\x00\x01'
CONTEXT = b'SmartTapo release signing Ed25519 current-user v1\x00'
MAX_KEY_SIZE = 16384


class SigningError(RuntimeError):
    """Only constant, nonsecret diagnostics may cross the CLI boundary."""


def check_path(path, *, new=False, private=False):
    """Reject links/reparse points before resolving; never follow a key alias."""
    path = Path(os.path.abspath(path))
    try:
        for item in [*reversed(path.parents), path]:
            try:
                info = item.lstat()
            except FileNotFoundError:
                if item != path:
                    raise SigningError('Output parent must already exist.')
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & 0x400
                    or info.st_nlink > 1):
                raise SigningError('Unsafe linked path rejected.')
            if item == path and new:
                raise SigningError('Refusing to overwrite an existing file.')
        resolved = path.resolve(strict=False)
        if private and (resolved == ROOT or ROOT in resolved.parents):
            raise SigningError('Private keys must remain outside the repository and build tree.')
        if private and any(part.lower() in {'build', 'dist', '_internal'} for part in resolved.parts):
            raise SigningError('Private keys must remain outside the repository and build tree.')
        return path
    except SigningError:
        raise
    except Exception:
        raise SigningError('Cannot validate signing path.') from None


def _write_new(path, content, created):
    # O_EXCL is essential: exists() followed by a normal write is not exclusive.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    identity = os.fstat(fd)
    created.append((path, identity.st_dev, identity.st_ino))
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _rollback(created):
    for path, device, inode in reversed(created):
        try:
            info = path.lstat()
            if info.st_dev == device and info.st_ino == inode and info.st_nlink == 1:
                path.unlink()
        except OSError:
            pass


def _windows_required():
    if sys.platform != 'win32':
        raise SigningError('Windows current-account DPAPI is required.')


def generate_keys(private, public, *, windows_account=False, password=None):
    created = []
    try:
        private = check_path(private, new=True, private=True)
        public = check_path(public, new=True)
        if private == public:
            raise SigningError('Distinct private and public paths are required.')
        if windows_account:
            _windows_required()
        elif not password:
            raise SigningError('A nonempty passphrase is required.')
        key = Ed25519PrivateKey.generate()
        public_bytes = key.public_key().public_bytes_raw()
        if windows_account:
            private_bytes = HEADER + _dpapi(CONTEXT + key.private_bytes_raw() + public_bytes)
        else:
            private_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.BestAvailableEncryption(password))
        # Revalidate immediately before publication; cleanup owns only O_EXCL files.
        check_path(private, new=True, private=True)
        check_path(public, new=True)
        _write_new(private, private_bytes, created)
        _write_new(public, public_bytes, created)
    except Exception:
        _rollback(created)
        raise SigningError('Key creation failed; existing files were preserved.') from None


def load_key(private, *, windows_account=False, password=None):
    try:
        private = check_path(private, private=True)
        with private.open('rb') as stream:
            raw = stream.read(MAX_KEY_SIZE + 1)
        if not raw or len(raw) > MAX_KEY_SIZE:
            raise ValueError()
        if windows_account:
            _windows_required()
            if not raw.startswith(HEADER) or len(raw) <= len(HEADER):
                raise ValueError()
            payload = _dpapi(raw[len(HEADER):], decrypt=True)
            if len(payload) != len(CONTEXT) + 64 or not payload.startswith(CONTEXT):
                raise ValueError()
            key = Ed25519PrivateKey.from_private_bytes(payload[len(CONTEXT):len(CONTEXT)+32])
            if key.public_key().public_bytes_raw() != payload[-32:]:
                raise ValueError()
        else:
            if raw.startswith(HEADER) or not password:
                raise ValueError()
            key = serialization.load_pem_private_key(raw, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError()
        return key
    except Exception:
        raise SigningError('Cannot open signing key in the selected mode.') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    gen = sub.add_parser('keygen', help='Create explicitly named new encrypted signing key')
    gen.add_argument('--private', required=True, type=Path)
    gen.add_argument('--public', required=True, type=Path)
    gen.add_argument('--windows-account', action='store_true', help='Use current-account Windows DPAPI, without a passphrase')
    sign = sub.add_parser('sign', help='Sign an explicitly supplied release EXE')
    sign.add_argument('--private', required=True, type=Path)
    sign.add_argument('--windows-account', action='store_true')
    sign.add_argument('--exe', required=True, type=Path)
    sign.add_argument('--version', required=True, type=int)
    sign.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        password = None if args.windows_account else getpass.getpass('Private key passphrase (not logged): ').encode('utf-8')
        if args.command == 'keygen':
            generate_keys(args.private, args.public, windows_account=args.windows_account, password=password)
            print('Encrypted local key created. Nothing published; independently approve the public key before pinning.')
            return
        if args.version < 1 or args.exe.name != 'SmartTapo-V' + str(args.version) + '.exe':
            raise SigningError('EXE filename must match numeric version.')
        size = args.exe.stat().st_size
        if not 0 < size <= 512 * 1024 * 1024:
            raise SigningError('EXE size rejected.')
        digest = hashlib.sha256()
        with args.exe.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        obj = dict(schema=1, repository=REPOSITORY, version=args.version, filename=args.exe.name,
                   size=size, sha256=digest.hexdigest())
        key = load_key(args.private, windows_account=args.windows_account, password=password)
        payload = json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        obj['signature'] = base64.b64encode(key.sign(payload)).decode('ascii')
        output = check_path(args.output, new=True)
        created = []
        try:
            _write_new(output, json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('ascii'), created)
        except Exception:
            _rollback(created)
            raise
        print('Signed local manifest created. No publishing performed.')
    except SigningError:
        raise
    except Exception:
        raise SigningError('Signing operation failed; check explicit input paths and permissions.') from None


if __name__ == '__main__':
    try:
        main()
    except SigningError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
