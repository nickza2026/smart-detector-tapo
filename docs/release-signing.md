# Offline signing operator procedure

Production-key creation was explicitly approved on 2026-11-08 at 22:02:22 Asia/Bangkok. A new current-Windows-account DPAPI Ed25519 key was created outside the repository; public-only trust pinning remains a separate integration step. These commands do not authorize publication.

Use a dedicated offline/operator Python environment with `requirements-updater.txt`. cryptography 50.0.2 was verified from public PyPI metadata on 2026-11-08. Do not modify the shared development environment.

Explicit local key creation (only after operator approval):

```powershell
python scripts/sign_update.py keygen --private 'X:\offline-signing\release-key.pem' --public 'X:\offline-signing\release-public.bin'
```

By default the private PEM key is encrypted with a prompted nonempty passphrase. Alternatively, on Windows use `--windows-account` on BOTH `keygen` and `sign`: it never prompts for a passphrase, and uses the source `desktop_storage._dpapi` helper only (no credential-store reads). DPAPI uses CURRENT_USER and UI_FORBIDDEN, never LOCAL_MACHINE. Its encrypted payload contains a versioned signing context, 32-byte raw private key and 32-byte public key; decryption strictly checks context, length and derived-public agreement. Wrong mode, non-Windows use and corruption fail closed with constant safe errors.

Keep private keys outside every repository/build tree. Parent directories must already exist, with no symlink, junction/reparse point or hardlink aliases. New private/public files are exclusively created and never overwrite existing files. A failed publication removes only files created by that invocation. Protect a new signing directory with an account-only DACL BEFORE generating a key; do not change permissions on existing profile or parent directories. The script never uploads anything or creates a backup. DPAPI is tied to the Windows account; copying the encrypted blob alone is NOT disaster recovery, and account loss or Windows reinstall may make it unrecoverable. Any portable recovery export requires separate explicit approval. No backup has been performed.

Approve the 32-byte raw public key independently, then pin those bytes in code; a key downloaded from the same unsigned release is NOT a trust root. Key rotation requires an independently authorized trust-root update. Never bundle, commit or log a private key, plaintext seed, passphrase or encrypted private blob.

Approved current-account creation used:

```powershell
python scripts/sign_update.py keygen --windows-account --private "$env:LOCALAPPDATA\SmartTapoSigning\release-ed25519-5e885c5bd41c47a0ad8b0902106baf70\release-key.dpapi" --public "$env:LOCALAPPDATA\SmartTapoSigning\release-ed25519-5e885c5bd41c47a0ad8b0902106baf70\release-public.bin"
```

Creation receipt (nonsecret, outside repository): `creation-receipt.json` in that same new directory. Created UTC `2026-11-08T15:06:15.6509147Z`. Directory owner and sole FullControl allow ACE are the current Windows account; inheritance is disabled on that directory only, and key files were verified to inherit the account-only DACL. New-key validation signed and verified only a disposable synthetic manifest, not a release artifact.

Public key (32-byte hex): `fc4f1f7eb74b1adfbc9e6047e2863d8071b6d17ce79dd230495e0d2fd6f93644`.
Public SHA-256 fingerprint: `48c5bea4bfc1e61a63b56ddb4ac5c865ca6033a49b686ee7f05f5bb127d8f98a`.

Explicit manifest generation:

```powershell
python scripts/sign_update.py sign --private 'X:\offline-signing\release-key.pem' --exe 'X:\delivery\SmartTapo-V45.exe' --version 45 --output 'X:\delivery\SmartTapo-update.json'
```

Tag must be `V45`; release assets must be exactly `SmartTapo-V45.exe` and `SmartTapo-update.json`. Manifest exact fields: `schema` integer 1, `repository` string `nickza2026/smart-detector-tapo`, `version` monotonic positive integer, `filename`, integer `size`, lowercase hex `sha256`, base64 `signature`. Ed25519 signs the other six fields serialized with Python `json.dumps(sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')`. Extra/duplicate fields, boolean-as-integer, replay, downgrade, mismatched filename/repository, missing/tampered signatures are rejected.

Do not ship an enabled installer yet. Windows process-handle ownership, same-volume atomic replace/recovery journal, GUI readiness/version acknowledgement and rollback are implemented and integrated, but fake-adapter tests are NOT native onefile upgrade/rollback acceptance. Production key approval/pinning and native lifecycle acceptance remain release gates. A successful signature/download or ordinary cold EXE smoke is NOT installation acceptance. Prior verified terminal-ready claims are archived automatically for subsequent updates while old EXE backups remain until explicit cleanup. Existing business/config/history/outbox data must never become part of a replacement transaction. Only the public key may be pinned by the integration owner; no public release publication is authorized.

Native lifecycle evidence was subsequently obtained with disposable TEST-ONLY
onefile V45/V46 variants and an ephemeral in-memory test key: successful upgrade,
startup exit before ACK with rollback, and native no-ACK timeout with rollback.
Real normal/rollback GUI windows were mapped offscreen without focus; production
engine/entrypoint code was unchanged. Receipt:
`.tools/v45-native-310e7b33c18947d187405f6b76699a5b/native-acceptance.json`.
This is engine-through-normal-GUI acceptance, not real-user-data or complete
button-click acceptance. Production key permission/pinning and actual release
publication remain separate gates. Runtime checks repeat every six hours while
the app is open; installation still requires explicit confirmation.
