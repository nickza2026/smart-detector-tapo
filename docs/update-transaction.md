# Executable update transaction

## Release gate

`updates.PINNED_PUBLIC_KEY` remains **None**. Both staging and the independently
running helper call the existing Ed25519 `updates.verify_manifest` using that
compiled-in key. Installation is disabled until separately approved key
provisioning. This implementation does not generate/read keys, sign, publish,
contact cameras/APIs, or open application configuration/business databases.

These are synthetic-fixture/fake-native-adapter tests, not evidence that a
packaged Windows upgrade has passed operator acceptance. A real packaged GUI
startup/rollback test remains necessary before enabling production installation.

## Integration contract (entrypoint owner)

Dispatch these fixed internal arguments before importing GUI/config/database
modules, and reject missing, duplicate or extra arguments in helper mode:

* `--update-helper ABS_PLAN --update-token HEX_TOKEN`: call
  `update_install.apply_update(plan, token)` and exit with its status.
* `--update-startup-plan ABS_PLAN --update-token HEX_TOKEN`: normal **visible** GUI
  startup, with all automatic capture/listener/API work suppressed. After the
  actual normal GUI has initialized successfully, call
  `acknowledge_startup(plan, token)`. A preflight or merely starting a process is
  not readiness. Reject malformed startup plans rather than falling back to an
  ordinary possibly automatic launch.
* `--update-restart`: accepted on both upgraded and rollback visible launches;
  suppress ALL automatic camera/API/capture activity. It is not windows-login.
  This flag is always included by the engine; never forward arbitrary old argv.

The old application **must already support all these flags before shipping the
first release capable of updating itself**, so rollback can restart that version.

Public API, all in `smart_tapo.update_install`:

```python
plan_path = prepare_update(
    current_exe_absolute_path, raw_signed_manifest_bytes, downloaded_exe_path,
    current_version=running_integer_version,  # optional consistency assertion
    config_path=explicit_config_absolute_path_or_none,
)
helper = start_helper(plan_path)  # returns owned Popen; hidden helper only
wait_for_arm(plan_path, helper=helper, timeout=15)
# Only now gracefully close the current normal GUI and exit the old process.
```

The token is in the fresh owned `plan.json`; `start_helper` and `wait_for_arm`
read/validate it when omitted. Internal helper/startup CLI calls supply their
explicit token. `prepare_update` returns a `Path`, not a command/string. A caller
must never execute anything from the JSON. Any raw signed manifest must preserve
the signed field values; reconstructing from `Manifest.payload` plus base64 of
`Manifest.signature` is supported by the signature canonicalization boundary.
The raw manifest is stored unchanged inside the owned stage.

Only an explicit original `--config` path is preserved, as the fixed pair
`['--config', absolute_path]`. No config file contents are read. No other user
arguments, secret arguments, windows-login flags, cwd, environment commands or
extra executable paths are serialized. Normal restarts use the installation
folder as cwd and are visible. The only environment modification is a **child
local** `PYINSTALLER_RESET_ENVIRONMENT=1` to prevent reuse of expiring onefile
extraction files; no global environment/registry/admin changes occur.

Do not close the old GUI when `start_helper` or `wait_for_arm` fails. A busy parent
is never killed; the helper exits without touching the executable. Cancellation
must not blindly remove a stage while a launch reservation/helper is live.

## Ownership and identity

An exclusively created `.smart-tapo-update.claim` in the target EXE's parent
excludes concurrent attempts. Acquisition failure leaves the old transaction
untouched. Stage names are random `.smart-tapo-update-<32 hex>` siblings of the
EXE, created exclusively; owner metadata contains a fresh 256-bit token. Only
fixed filenames in this owned directory are accepted. Links, reparse points,
hardlinked files, UNC/device/ADS/relative paths and linked ancestors are rejected.
There is no recursive arbitrary-directory cleanup. All rename operations stay on
the EXE parent's volume. MZ/EXE checks are format sanity only, never authenticity:
full signed SHA-256 and size are mandatory for the candidate.

The helper is a copy of the running trusted EXE, not the downloaded EXE. It checks
its own full hash, target's expected full hash, compiled build version, signed
manifest/version and candidate hash/size before any rename. The helper binds
native HANDLEs for the old GUI PID, exact image path and creation time **before**
writing its armed ACK. Waiting uses those handles, not polling recycled PIDs.

PyInstaller onefile introduces a bootloader parent. During staging its same-image
native parent identity is recorded; the helper independently validates ancestry,
binds it before arming, and waits for both old GUI and bootloader to exit. During
new startup the ACK's PID must bind either to the exact Popen process HANDLE or a
native direct worker of that still-live bootloader, with the same executable
path and non-older creation time. The resulting worker HANDLE is the one used
for ACK health/grace. No arbitrary ACK PID is trusted. For a no-ACK timeout the
adapter freezes the owned bootloader, enumerates only native direct children,
binds/validates each handle before terminating, and then stops the bootloader.
If enumeration/ownership/termination fails after suspension, the exact owned
bootloader HANDLE is resumed unless already terminated; it is never left frozen
merely because enumeration failed. It fails closed rather than killing a PID
or deleting an unknown executable. No real process is launched by these tests.

## Journal and recovery

Atomic fsynced JSON journal transitions:

`prepared -> armed -> parent_exited -> backed_up -> swapped -> ready`

On normal startup failure: `swapped -> rolled_back`. An `error` state is reserved;
ambiguous failures deliberately retain the last authoritative journal rather
than overwrite evidence. `apply_update` returns 0 for committed GUI readiness,
1 for completed rollback/restart, 2 for blocked/ambiguous/manual recovery.

The original executable becomes `backup.exe` before the candidate is atomically
installed. The helper accepts readiness only from the owned launched GUI HANDLE,
with matching token and image, then requires three additional seconds alive.
Only after that does the journal become `ready`. The verified original backup is
retained. On timeout/failure it stops only its owned new launch, validates both
hashes, atomically restores the backup, and starts the old UI visibly with
`--update-restart` (no camera/API auto-start). If the target has become an unknown
file, rollback fails closed and leaves both it and the verified backup intact.

`recover_update(plan_path, token)` is an explicit **trusted self-copy helper**
operation, not automatic startup cleanup. It validates ownership/signature and
checks old/helper/new process identities are no longer live. It reconciles file
hashes, including crashes between rename and journal write. If a verified backup
exists and target is absent or the verified candidate, recovery restores old;
unknown/malformed layouts are retained for manual intervention. A valid owned
interrupted journal temporary file can be discarded; malformed partial metadata
is never guessed at. Recovery never swaps forward based only on a stale state.
No automatic recovery CLI flag is required; any UI exposing it must dispatch it
before normal application imports and retain the same strict fixed-argument
boundary.

`cancel_update(plan_path, token)` validates ownership, target hash, journal state
and all recorded process/launch reservations. It deletes only known owned files
for safe cancelled/rolled-back transactions. It refuses unknown artifacts, live
helpers, unresolved launch reservations, active/ambiguous states, and any retained
backup by default. After explicit operator consent, a healthy completed
transaction can be removed using `cancel_update(..., discard_healthy_backup=True)`;
this still verifies the retained old hash and requires helper/bootloader exit.
The next update automatically archives a prior claim ONLY after terminal `ready`,
owner/token, signed prior manifest, compiled running target version, current target
hash/size, retained backup and startup proof are verified. Windows no-replace
rename moves the claim into that old stage as `completed.json`; the backup remains.
Archived explicit cleanup validates this proof without touching a newer active
claim or requiring that the executable still match an obsolete target hash.
Prepared/armed/interrupted or ambiguous claims remain blocked. A crash before
archival retains the claim; after archival the retained proof permits future
explicit backup cleanup. No general stale-artifact scavenger is provided.

This is executable rollback only. It does **not** roll back database migrations;
no database/config/profile/history/outbox file is modified by the engine. The new
GUI readiness integration must avoid irreversible business-data migrations before
acknowledging health. Same-user process injection or an attacker replacing files
between native filesystem calls is outside the protection offered by inherited
installation-directory ACLs; a locked-down installer/service would be a separate
security boundary.

## Focused verification

Using the existing isolated verifier interpreter (no environment mutations):

```text
.tools\update-verifier-v45\Scripts\python.exe -m unittest discover -s tests -p test_update_install.py -v
.tools\update-verifier-v45\Scripts\python.exe -m unittest discover -s tests -p test_update_windows.py -v
```

No full test suite, package build, native launch/kill, live user config or hardware
is used by these transaction tests. Signature cryptography is owned/tested by
`updates.py`; transaction tests mock only that boundary, never create keypairs.
