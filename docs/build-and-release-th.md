# วิธี build Windows EXE และออกเวอร์ชันใหม่บน GitHub

คู่มือนี้อ้างอิง `scripts/build-windows.ps1`, `scripts/smoke_packaged.py`, `scripts/sign_update.py`, `smart_tapo/updates.py` และ `.github/workflows/build-windows-exe.yml` ใช้ PowerShell บน Windows และ Python **3.12 x64**

การเขียนคู่มือหรืออนุมัติแก้โค้ดไม่ใช่การอนุมัติเผยแพร่ ต้องตกลงรายการงานในรุ่นและได้รับอนุมัติรอบนั้นก่อน push/สร้าง Release โดยเฉพาะเมื่อมีการเพิ่มงานหรือผู้ใช้ขอพักการเผยแพร่

## 1. ภาพรวมและสิ่งที่ต้องเตรียม

ลำดับคือ **เลือก source → เปลี่ยนเลขรุ่น → ทดสอบ/build → ตรวจ EXE → เซ็น manifest → สร้าง draft → ตรวจไฟล์บน GitHub → ยืนยันเผยแพร่ → ทดลองดาวน์โหลด**

มีทาง build สองแบบ:

- **ในเครื่อง:** สคริปต์ทดสอบและสร้าง `dist/SmartTapo.exe`
- **GitHub Actions:** workflow สร้าง artifact ของ commit ที่ push แล้ว ไม่รวมไฟล์ที่แก้เฉพาะในเครื่อง

workflow ปัจจุบันมีสิทธิ์ `contents: read` และ **ไม่ได้สร้าง Release, เซ็น manifest หรือเก็บ private signing key** ให้เอง ทั้งสองทางจึงต้องผ่านขั้นตอนเซ็นและออก Release ด้านล่าง

เตรียม Git, GitHub CLI (`gh`) ที่ใช้บัญชีซึ่งได้รับสิทธิ์ repo อยู่แล้ว และกุญแจลงนามเดิมสำหรับผู้ดูแล หากคำสั่ง GitHub ล้มเหลวให้หยุดอ่านข้อผิดพลาด ไม่ค้นหา token/private key เพิ่ม ไม่เปลี่ยนบัญชีหรือ transport เพื่อบังคับให้ผ่าน

ตัวอย่างต่อไปนี้ใช้ **V56 เป็นเลขสมมติ** ไม่ได้ยืนยันว่า V55 เผยแพร่แล้ว ให้เลือกเลขจำนวนเต็มที่สูงกว่ารุ่นสาธารณะจริง และใช้ PowerShell session เดียวกันสำหรับตัวแปรต่อเนื่อง:

```powershell
Set-Location 'D:\works\smart-detector-tapo'
$ErrorActionPreference = 'Stop'
$Repo = 'nickza2026/smart-detector-tapo'
$Version = 56
$Tag = "V$Version"
$DeliveryRoot = Join-Path (Split-Path (Get-Location).Path -Parent) 'smart-tapo-deliveries'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed: $LASTEXITCODE" }
}

Invoke-Checked 'git' @('status', '--short')
Invoke-Checked 'gh' @('release', 'list', '--repo', $Repo, '--limit', '20')
```

## 2. ตรึง source และเปลี่ยนเลขรุ่น

ตรวจ staged/unstaged changes และงานค้างก่อน ห้าม `reset`, `clean`, ทับไฟล์ หรือสลับไป main จนงานของคนอื่นหาย การออก source รุ่นใหม่ตามปกติใช้ topic branch ของ delivery batch และ commit เฉพาะงานที่ตรวจพร้อมส่งแล้ว ไม่รวม credential, sample ส่วนตัว, ฐานข้อมูล, `.tools`, build output หรือ private key

จุดกำหนดเลข desktop คือ `smart_tapo/diagnostics.py`:

```python
DIAGNOSTIC_BUILD = '2026-11-19.56'
APP_VERSION = 'v' + DIAGNOSTIC_BUILD.rsplit('.', 1)[1]
```

วันที่ข้างหน้าเป็นวันที่ build ส่วนเลขหลังจุดสุดท้ายต้องตรง `$Version` การเปลี่ยน Git tag หรือชื่อ EXE อย่างเดียวไม่เปลี่ยนรุ่นในโปรแกรม `pyproject.toml` ที่เป็น `0.1.0` ไม่ใช่เลข desktop updater

หลังรวมการเปลี่ยนรุ่นและงานที่อนุมัติเป็น commit แล้ว ตรึง commit ที่จะ build:

```powershell
$Dirty = git status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect worktree' }
if ($Dirty) { throw 'มีงานยังไม่ commit: ตกลง source ของรุ่นให้ชัดก่อน build เพื่อเผยแพร่' }
$Commit = (git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve commit' }
$Branch = (git branch --show-current).Trim()
if (-not $Branch) { throw 'ใช้ branch ที่ตกลงไว้ก่อน ไม่ publish จาก detached HEAD โดยไม่ตรวจสอบ' }
```

อย่าแก้ source/tests/assets ระหว่าง build และทดสอบ หากมีการเปลี่ยนหลังผ่านการตรวจ ต้อง build และตรวจ snapshot ใหม่

ถ้าตกลงเผยแพร่ binary จาก dirty snapshot เป็นกรณีพิเศษ ต้องมี snapshot/hash และหลักฐานทดสอบของ snapshot นั้น รวมทั้งอธิบายว่า GitHub Source ZIP/TAR ไม่ใช่ source ที่ build ห้ามอ้าง HEAD หรือ CI artifact ว่ารวมไฟล์ dirty ไปด้วย ขั้นตอนปกติในคู่มือนี้เลือก **clean committed source** เพื่อให้ตรวจย้อนกลับได้

## 3A. Build ในเครื่อง

สคริปต์สร้าง `.venv-build`, ติดตั้ง dependencies จาก `requirements-build.txt`, รัน `pip check`, ชุด unittest ทั้งหมด, PyInstaller แบบ onefile/windowed และ packaged GUI smoke ก่อนคัดลอก README ไป `dist`

`--clean --noconfirm` อาจทับ output ใน `build`/`dist` ให้เก็บ artifact รุ่นก่อนที่ต้องใช้ไว้คนละที่ก่อนเริ่ม ไม่ใช้โฟลเดอร์ข้อมูลจริงของโปรแกรมเป็นโฟลเดอร์ build

```powershell
.\scripts\build-windows.ps1
$Python = (Resolve-Path '.venv-build\Scripts\python.exe').Path
$BuiltExe = (Resolve-Path 'dist\SmartTapo.exe').Path
$BuildOrigin = 'local scripts/build-windows.ps1'
```

หากต้องระบุ Python เอง:

```powershell
.\scripts\build-windows.ps1 -PythonExecutable 'C:\Python312\python.exe'
```

ใช้คำสั่ง build เพียงแบบเดียวข้างต้น สคริปต์ตรวจว่า Python เป็น 3.12 และต้องทำงานบน Windows ไม่ใช่ cross-compile จาก Linux/macOS

GUI smoke ใช้ข้อมูลสังเคราะห์และตรวจ `real_notifications = 0` ไม่ถือว่าผ่านกล้อง/Notify API/มือถือจริง การทดสอบหน้าต่างอัตโนมัติต้องอยู่พ้นขอบทุกจอโดยไม่แย่ง focus และยังตรวจ mapping/render จริง

หากต้องตรวจ packaged EXE ซ้ำโดยไม่ build ใหม่:

```powershell
Invoke-Checked $Python @('-m', 'scripts.smoke_packaged', '--dist', 'dist')
```

หากมีการเปลี่ยนเสียง/ประวัติ/ส่ง API ให้ตรวจ regression ที่เกี่ยวข้องเพิ่มเติมตามงานนั้น รวม negative cases และคลิปที่ได้รับอนุญาต การ build ผ่านไม่ใช่หลักฐานว่าปัญหาทางธุรกิจทุกกรณีหาย ห้ามใช้คีย์จริงหรือส่งแจ้งเตือนจริงเพื่อแทน synthetic smoke

## 3B. Build ผ่าน GitHub Actions แทนในเครื่อง

ใช้เมื่อ commit/branch ที่ต้องการอยู่บน GitHub แล้ว การ push ต้องได้รับอนุมัติแยกก่อนเสมอ workflow รันได้จาก `workflow_dispatch` และ push ที่ตรง path filter ใน YAML ปัจจุบัน **ไม่มี trigger `pull_request`** และการแก้เฉพาะ `docs/` ไม่อยู่ใน path filter

```powershell
Invoke-Checked 'gh' @('workflow', 'run', 'build-windows-exe.yml', '--repo', $Repo, '--ref', $Branch)
Invoke-Checked 'gh' @('run', 'list', '--repo', $Repo, '--workflow', 'build-windows-exe.yml', '--branch', $Branch, '--limit', '10')
```

เลือก run ของ commit ที่ต้องการ ไม่เลือก artifact ล่าสุดโดยไม่ตรวจ SHA:

```powershell
$RunId = Read-Host 'Run ID ของ commit ที่จะออกเวอร์ชัน'
if ($RunId -notmatch '^\d+$') { throw 'Run ID ต้องเป็นตัวเลข' }
Invoke-Checked 'gh' @('run', 'watch', $RunId, '--repo', $Repo, '--exit-status')
$RunText = gh api "repos/$Repo/actions/runs/$RunId"
if ($LASTEXITCODE -ne 0) { throw 'Cannot read workflow result' }
$Run = ($RunText -join "`n") | ConvertFrom-Json
if ($Run.status -ne 'completed' -or $Run.conclusion -ne 'success' -or $Run.head_sha -ne $Commit) {
    throw 'Run ยังไม่สำเร็จหรือไม่ใช่ commit ที่ตรึงไว้'
}
$Artifact = "smart-tapo-desktop-windows-x64-$($Run.run_number)-$($Run.run_attempt)"
New-Item -ItemType Directory -Path $DeliveryRoot -Force | Out-Null
$DownloadDir = Join-Path $DeliveryRoot "release-input-$Tag-$RunId"
if (Test-Path -LiteralPath $DownloadDir) { throw 'ใช้โฟลเดอร์ดาวน์โหลดใหม่ ไม่รวมไฟล์เก่า' }
Invoke-Checked 'gh' @('run', 'download', $RunId, '--repo', $Repo, '--name', $Artifact, '--dir', $DownloadDir)
$BuiltExe = (Resolve-Path (Join-Path $DownloadDir 'SmartTapo.exe')).Path
$BuildOrigin = "GitHub Actions run $RunId attempt $($Run.run_attempt)"
```

artifact มี `SmartTapo.exe` กับ `README.md` และเก็บ 14 วันตาม workflow ปัจจุบัน ดาวน์โหลดและตรวจ **ไฟล์นี้** ต่อ ไม่ build ใหม่แล้วนำอีกไฟล์ไปอ้างว่าเป็น artifact ที่ผ่าน CI

เตรียม Python 3.12 ที่มี dependencies และ PyInstaller เพื่อใช้ตรวจ archive ด้านล่าง เช่น `.venv-build\Scripts\python.exe` จากทาง 3A หรือ virtualenv สำหรับตรวจโดยเฉพาะ:

```powershell
New-Item -ItemType Directory -Path $DeliveryRoot -Force | Out-Null
$CheckEnv = Join-Path $DeliveryRoot 'check-env-py312'
Invoke-Checked 'py' @('-3.12', '-m', 'venv', $CheckEnv)
$Python = (Resolve-Path (Join-Path $CheckEnv 'Scripts\python.exe')).Path
Invoke-Checked $Python @('-m', 'pip', 'install', '-r', 'requirements-build.txt')
```

## 4. จัดชุด Release และตรวจรุ่นภายใน EXE

หลังจบ 3A หรือ 3B ให้ใช้ `$BuiltExe`, `$Python`, `$Commit`, `$BuildOrigin` จากทางที่เลือก:

```powershell
if (git status --porcelain) { throw 'Source เปลี่ยนหลังตรึง commit ให้ตรวจใหม่' }
$CurrentCommit = Invoke-Checked 'git' @('rev-parse', 'HEAD')
if ($CurrentCommit.Trim() -ne $Commit) { throw 'HEAD เปลี่ยนหลังตรึง commit ให้ตรวจใหม่' }
New-Item -ItemType Directory -Path $DeliveryRoot -Force | Out-Null
$ReleaseDir = Join-Path $DeliveryRoot "release-output-$Tag"
if (Test-Path -LiteralPath $ReleaseDir) { throw 'ห้ามทับชุด release เดิม ให้ตรวจหรือเลือกพื้นที่ใหม่' }
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
$ExeName = "SmartTapo-$Tag.exe"
$ReleaseExe = Join-Path $ReleaseDir $ExeName
Copy-Item -LiteralPath $BuiltExe -Destination $ReleaseExe
$ExeHash = (Get-FileHash -LiteralPath $ReleaseExe -Algorithm SHA256).Hash.ToLowerInvariant()
$ExeSize = (Get-Item -LiteralPath $ReleaseExe).Length

@'
import sys, types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
exe, version = Path(sys.argv[1]), int(sys.argv[2])
pyz = CArchiveReader(str(exe)).open_embedded_archive('PYZ.pyz')
module = types.ModuleType('release_diagnostics')
exec(pyz.extract('smart_tapo.diagnostics'), module.__dict__)
assert module.APP_VERSION == 'v' + str(version)
assert module.DIAGNOSTIC_BUILD.rsplit('.', 1)[1] == str(version)
print('PACKAGED_BUILD=' + module.DIAGNOSTIC_BUILD)
'@ | & $Python - $ReleaseExe $Version
if ($LASTEXITCODE -ne 0) { throw 'EXE ภายในไม่ตรงเลขรุ่น ห้ามแค่เปลี่ยนชื่อไฟล์แล้วส่ง' }
```

สร้าง `RELEASE-NOTES-th.md` ใน `$ReleaseDir` โดยบันทึกงานทั้งหมดที่ส่ง พฤติกรรมที่เปลี่ยน ผลทดสอบจริง/จำนวน skip ข้อจำกัด และวิธีอัปเดต ไม่ใส่คีย์ URL ที่มี credentials, ข้อมูลลูกค้า หรืออ้างติดตั้งบน VPS/มือถือสำเร็จหากยังไม่ได้ตรวจ

สร้าง provenance ของ binary โดยไม่ใส่ private key หรือข้อมูลตัวอย่างจริง:

```powershell
$Provenance = [ordered]@{
    application = 'SmartTapo'
    version = $Version
    repository = $Repo
    source_commit = $Commit
    source_changes_committed = $true
    build_origin = $BuildOrigin
    executable_sha256 = $ExeHash
    executable_size = $ExeSize
    verification_notes = 'RELEASE-NOTES-th.md'
    production_verified = $false
}
$Utf8 = [System.Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText((Join-Path $ReleaseDir 'BUILD-PROVENANCE.json'),
    ($Provenance | ConvertTo-Json -Depth 6), $Utf8)
```

## 5. เซ็น update manifest ด้วยกุญแจเดิม

ใช้ Python environment ของผู้ลงนามที่มี `requirements-updater.txt` และกุญแจเดิม **นอก repository/build tree** สำหรับ DPAPI ต้องเป็นบัญชี Windows เดิมที่เปิดกุญแจได้ ไม่ copy blob ไป runner แล้วคาดว่าจะถอดได้

```powershell
$SigningPython = Read-Host 'พาธ python.exe ของ signing environment ที่เตรียมไว้'
$KeyPath = Read-Host 'พาธไฟล์ release-key.dpapi เดิมที่ได้รับอนุมัติ (ไม่ใช่เนื้อหาคีย์)'
$ManifestPath = Join-Path $ReleaseDir 'SmartTapo-update.json'
Invoke-Checked $SigningPython @('scripts/sign_update.py', 'sign', '--windows-account',
    '--private', $KeyPath, '--exe', $ReleaseExe, '--version', "$Version", '--output', $ManifestPath)
```

หากใช้ encrypted PEM ที่ได้รับอนุมัติ ให้ตัด `--windows-account` และใช้ path ของ PEM เดิม โปรแกรมถาม passphrase แบบไม่แสดงค่า ไม่ใส่ passphrase ในคำสั่งหรือเอกสาร

ห้ามสร้าง key ใหม่เพื่อแก้ปัญหาเซ็นไม่ผ่าน: updater รุ่นเก่าฝัง public key เดิมอยู่ การหมุนกุญแจต้องมีแผนและอนุมัติต่างหาก สคริปต์ไม่มี subcommand `verify`; ใช้ตัวตรวจที่อยู่ใน EXE จริง:

```powershell
@'
import hashlib, sys, types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
exe, version = Path(sys.argv[1]), int(sys.argv[2])
pyz = CArchiveReader(str(exe)).open_embedded_archive('PYZ.pyz')
name = 'smart_tapo.updates'
module = types.ModuleType(name)
module.__package__ = 'smart_tapo'
sys.modules[name] = module
exec(pyz.extract(name), module.__dict__)
assert hashlib.sha256(module.PINNED_PUBLIC_KEY).hexdigest() == '48c5bea4bfc1e61a63b56ddb4ac5c865ca6033a49b686ee7f05f5bb127d8f98a'
manifest = module.verify_manifest((exe.parent / 'SmartTapo-update.json').read_bytes(), version - 1)
with exe.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
assert (manifest.version, manifest.filename, manifest.size, manifest.sha256) == (
    version, exe.name, exe.stat().st_size, digest)
assert 'scripts.sign_update' not in pyz.toc
print('PACKAGED_PIN_SIGNATURE_SIZE_SHA256_VERIFIED')
'@ | & $Python - $ReleaseExe $Version
if ($LASTEXITCODE -ne 0) { throw 'ลายเซ็นหรือ EXE ไม่ตรง ห้ามเผยแพร่' }
```

นี่เป็น **Ed25519 signature ของ update manifest ไม่ใช่ Windows Authenticode signing ของ EXE** ไม่ได้ยืนยัน SmartScreen reputation หรือการติดตั้ง/rollback ของ artifact นี้บนเครื่องจริง

## 6. ตรวจชุดไฟล์ก่อน upload

ชุดที่แนบมีห้าไฟล์ ไม่แนบ source snapshot ส่วนตัว คลิปจริง ฐานข้อมูล config ที่มีคีย์ กุญแจส่วนตัว หรือ DPAPI blob:

```powershell
$Names = @($ExeName, 'SmartTapo-update.json', 'BUILD-PROVENANCE.json',
           'RELEASE-NOTES-th.md', 'SHA256SUMS.txt')
if (-not (Test-Path -LiteralPath (Join-Path $ReleaseDir 'RELEASE-NOTES-th.md'))) {
    throw 'เขียน release notes และผลทดสอบจริงก่อน'
}
$SumLines = foreach ($Name in $Names | Where-Object { $_ -ne 'SHA256SUMS.txt' }) {
    $Hash = (Get-FileHash -LiteralPath (Join-Path $ReleaseDir $Name) -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $Name"
}
[IO.File]::WriteAllLines((Join-Path $ReleaseDir 'SHA256SUMS.txt'), $SumLines, [Text.Encoding]::ASCII)
$Actual = @(Get-ChildItem -LiteralPath $ReleaseDir -Force | Select-Object -ExpandProperty Name)
if (Compare-Object ($Names | Sort-Object) ($Actual | Sort-Object)) { throw 'ชุดไฟล์ไม่ตรง allowlist' }
```

ตรวจเนื้อหา notes/provenance/manifest และประวัติ commit ที่จะเผยแพร่ ไม่ใช่แค่ชื่อไฟล์ การลบ secret ใน commit ล่าสุดไม่ลบ secret ออกจาก history ถ้าพบให้หยุดเพื่อขอแผนแก้ก่อน push/tag

## 7. สร้าง draft บน GitHub

**ทำหลังได้รับอนุมัติ batch นี้แล้ว** source commit ต้องอยู่บน remote และตรวจประวัติที่จะออกเรียบร้อย ไม่อัปโหลด source dirty ผ่าน `git add .` โดยอัตโนมัติ

ตรวจว่าเลขรุ่นยังไม่ถูกใช้:

```powershell
Invoke-Checked 'gh' @('release', 'list', '--repo', $Repo, '--limit', '100')
$ExistingTag = git ls-remote --tags origin "refs/tags/$Tag" "refs/tags/$Tag^{}"
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect remote tag' }
if ($ExistingTag) { throw 'Tag นี้มีแล้ว หยุดตรวจ ไม่ย้าย tag หรือใช้ --clobber' }
$AssetPaths = @($Names | ForEach-Object { Join-Path $ReleaseDir $_ })
$CreateArgs = @('release', 'create', $Tag) + $AssetPaths + @('--repo', $Repo,
    '--target', $Commit, '--draft', '--title', "SmartTapo $Tag",
    '--notes-file', (Join-Path $ReleaseDir 'RELEASE-NOTES-th.md'))
Invoke-Checked 'gh' $CreateArgs
```

Release ที่เป็น draft ยังไม่ใช่ public/latest และ URL อาจเป็น `untagged-...` ตาม GitHub อย่าบอกว่าผู้ใช้ดาวน์โหลดอัปเดตได้แล้วในขั้นนี้ หาก upload ล้มเหลวให้หยุดรายงานสถานะที่ทราบ ไม่สร้าง release ซ้ำ/repair authentication อัตโนมัติ

## 8. ตรวจไฟล์บน server แล้วจึงเปิดเผยแพร่

ตรวจ hash **ทุกไฟล์บน GitHub** ไม่ใช่อาศัยเพียง exit code ของ upload:

```powershell
$DraftText = gh api "repos/$Repo/releases/tags/$Tag"
if ($LASTEXITCODE -ne 0) { throw 'Cannot verify draft' }
$Draft = ($DraftText -join "`n") | ConvertFrom-Json
if (-not $Draft.draft -or $Draft.prerelease -or $Draft.target_commitish -ne $Commit) {
    throw 'Draft หรือ source target ไม่ตรง'
}
if (Compare-Object ($Names | Sort-Object) ($Draft.assets.name | Sort-Object)) {
    throw 'Asset บน GitHub ไม่ครบหรือมีไฟล์เกิน'
}
foreach ($Asset in $Draft.assets) {
    $Local = Join-Path $ReleaseDir $Asset.name
    $Hash = (Get-FileHash -LiteralPath $Local -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Asset.state -ne 'uploaded' -or $Asset.size -ne (Get-Item -LiteralPath $Local).Length -or
        $Asset.digest -ne "sha256:$Hash") { throw 'Server digest/size/state ไม่ตรง ห้าม publish' }
}
```

หาก server ไม่คืน digest ให้หยุดตรวจเพิ่ม ไม่ข้าม assertion ขั้นนี้ หลังทุก gate ผ่านและอนุมัติให้เปิดสาธารณะ:

```powershell
Invoke-Checked 'gh' @('release', 'edit', $Tag, '--repo', $Repo, '--draft=false', '--latest')
Invoke-Checked 'gh' @('release', 'view', $Tag, '--repo', $Repo)
```

Source ZIP/TAR ที่ GitHub สร้างอัตโนมัติอ้างอิง commit ของ tag ไม่ใช่ไฟล์ในเครื่องที่ยังไม่ commit การเปลี่ยน notes หรือ tag หลังทดสอบไม่ใช่วิธีทำให้ source ที่ผิดตรงกับ binary

## 9. ตรวจ public discovery และดาวน์โหลดเต็มไฟล์

หลัง publish ให้ใช้ตัว updater จาก EXE รุ่นที่เพิ่งเผยแพร่ ตรวจ public endpoint แบบไม่ส่ง API key, ดาวน์โหลดเต็มไฟล์และเทียบขนาด/hash ไม่ต้องติดตั้งหรือเปิดกล้อง:

```powershell
@'
import hashlib, sys, tempfile, threading, types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
exe, version = Path(sys.argv[1]), int(sys.argv[2])
pyz = CArchiveReader(str(exe)).open_embedded_archive('PYZ.pyz')
name = 'smart_tapo.updates'
module = types.ModuleType(name)
module.__package__ = 'smart_tapo'
sys.modules[name] = module
exec(pyz.extract(name), module.__dict__)
stop = threading.Event()
manifest, url = module.ReleaseClient().check(version - 1, stop)
assert manifest.version == version and manifest.filename == exe.name
with exe.open('rb') as stream:
    expected = hashlib.file_digest(stream, 'sha256').hexdigest()
assert manifest.sha256 == expected and manifest.size == exe.stat().st_size
with tempfile.TemporaryDirectory(prefix='release-public-check-') as directory:
    downloaded = Path(directory) / manifest.filename
    module.download(manifest, url, downloaded, stop)
    with downloaded.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected
    assert downloaded.stat().st_size == exe.stat().st_size
print('ANONYMOUS_PUBLIC_SIGNATURE_AND_FULL_DOWNLOAD_VERIFIED; NOT_INSTALLED')
'@ | & $Python - $ReleaseExe $Version
if ($LASTEXITCODE -ne 0) { throw 'Public verification ไม่ผ่าน ให้รายงานก่อนบอกว่าอัปเดตพร้อมใช้' }
```

ถ้าต้องยืนยันความเข้ากันได้กับรุ่นเก่า ให้ใช้โมดูล updater ที่สกัดจาก **EXE รุ่นเก่านั้นจริง** ซึ่งตรวจ hash ของ baseline แล้ว ไม่ใช้ source ปัจจุบันแทน การดาวน์โหลดสำเร็จไม่ใช่หลักฐานว่าปุ่มติดตั้ง, rollback, Notify API หรือเสียงมือถือทำงานครบ

## 10. สิ่งที่ส่งมอบและการติดตั้ง

รายงาน URL `https://github.com/nickza2026/smart-detector-tapo/releases/tag/V56` โดยแทนเลขจริง พร้อม SHA256/ขนาด EXE, commit/run ที่ build, ผลและข้อจำกัดของการทดสอบ และผล public download

ก่อนผู้ใช้ติดตั้ง รอให้ไม่มีการเติมจริงและหยุดงานก่อน ใช้ตัวอัปเดตพร้อมการยืนยันของผู้ใช้ ไม่ล้าง config/profiles/history/outbox/DB/WAL/SHM เพื่อหลบตัวบล็อก เก็บ EXE เก่าสำหรับย้อนกลับ และตรวจหัวหน้าต่างหลังเปิดใหม่ การเปลี่ยน EXE อาจทำให้ต้องยืนยัน profile/startup binding ใหม่ตามขอบเขตที่ผูกไว้

รายละเอียด trust root และรูปแบบ key อยู่ใน [release-signing.md](release-signing.md) ส่วนพฤติกรรมและข้อจำกัดของ updater ดู [public-updates-th.md](public-updates-th.md) และ [update-transaction.md](update-transaction.md) เอกสารเก่าอาจระบุ milestone/เลขรุ่นในอดีต ไม่ใช้แทนผลตรวจของ artifact รอบนี้
