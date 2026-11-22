param(
    [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{40}$')][string]$Commit,
    [Parameter(Mandatory)][string]$AppDir
)

$ErrorActionPreference = 'Stop'
chcp 65001 > $null
$env:PYTHONUTF8 = '1'

function Read-Digest([string]$Path) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToBase64String($hash.ComputeHash([IO.File]::ReadAllBytes($Path)))
    } finally {
        $hash.Dispose()
    }
}

Push-Location -LiteralPath $AppDir
try {
    $app = (Get-Location).Path
    $python = Join-Path $app 'venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath 'main.py' -PathType Leaf)) {
        throw 'Укажите папку бота с main.py и venv.'
    }
    $stage = Join-Path $env:TEMP ('yandex-bot-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $zip = Join-Path $stage 'source.zip'
    Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/Stonkix/yandex-bot/zip/$Commit" -OutFile $zip
    & $python -m zipfile -e $zip $stage
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось распаковать архив GitHub.' }
    $source = Join-Path $stage "yandex-bot-$Commit"
    $sourcePackage = Join-Path $source 'yandexbot'
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePackage 'bot.py') -PathType Leaf)) {
        throw 'В архиве GitHub нет пакета yandexbot.'
    }

    $updates = @(
        Get-ChildItem -LiteralPath $sourcePackage -File -Recurse |
            Where-Object { $_.Extension -in '.py', '.html', '.css' } |
            ForEach-Object {
                $relative = 'yandexbot\' + $_.FullName.Substring($sourcePackage.Length + 1)
                $destination = Join-Path $app $relative
                $exists = Test-Path -LiteralPath $destination -PathType Leaf
                if (-not $exists -or (Read-Digest $_.FullName) -ne (Read-Digest $destination)) {
                    [pscustomobject]@{ Relative = $relative; Source = $_.FullName; Destination = $destination; Exists = $exists }
                }
            }
    )
    if (-not $updates.Count) {
        Write-Output "Код $Commit уже установлен."
        return
    }

    pm2 stop astral_bot
    if ($LASTEXITCODE -ne 0) { throw 'PM2 не смог остановить бота.' }
    try {
        $backup = Join-Path (Split-Path -Parent $app) ('yandex-bot-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        New-Item -ItemType Directory -Path $backup | Out-Null
        foreach ($item in $updates) {
            if ($item.Exists) {
                $saved = Join-Path $backup $item.Relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
                Copy-Item -LiteralPath $item.Destination -Destination $saved
            }
        }

        @'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

for name in ("admin.db", "prices.db", "dadata.db"):
    path = Path("stat", name).resolve()
    if path.is_file():
        with (
            closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(Path(sys.argv[1], name))) as backup,
        ):
            source.backup(backup)
'@ | & $python - $backup
        if ($LASTEXITCODE -ne 0) { throw 'Резервное копирование SQLite не удалось.' }
        Write-Output "Резервная копия: $backup"

        $applied = @()
        try {
            foreach ($item in $updates) {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $item.Destination) | Out-Null
                $applied += $item
                Copy-Item -LiteralPath $item.Source -Destination $item.Destination -Force
            }
            & $python main.py --check
            if ($LASTEXITCODE -ne 0) { throw 'Проверка конфигурации не прошла.' }
        } catch {
            foreach ($item in $applied) {
                if ($item.Exists) {
                    Copy-Item -LiteralPath (Join-Path $backup $item.Relative) -Destination $item.Destination -Force
                } elseif (Test-Path -LiteralPath $item.Destination -PathType Leaf) {
                    Remove-Item -LiteralPath $item.Destination -Force
                }
            }
            throw
        }
    } finally {
        pm2 restart astral_bot
        if ($LASTEXITCODE -ne 0) { throw 'PM2 не смог запустить бота.' }
    }
    Write-Output "Обновлено файлов: $($updates.Count). Коммит: $Commit"
    pm2 status astral_bot
} finally {
    Pop-Location
}
