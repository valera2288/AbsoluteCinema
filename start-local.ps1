[CmdletBinding()]
param(
    [switch]$ResetDatabasePassword
)

$ErrorActionPreference = 'Stop'
$localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$manageScript = Join-Path $PSScriptRoot 'manage.py'
if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
    throw 'Виртуальное окружение не найдено. Подготовь его по README.md.'
}

function New-LocalSecureString([string]$Value) {
    $secure = [Security.SecureString]::new()
    foreach ($character in $Value.ToCharArray()) {
        $secure.AppendChar($character)
    }
    $secure.MakeReadOnly()
    return $secure
}

$profileDirectory = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'AbsoluteCinema'
$profileFile = Join-Path $profileDirectory 'local-dev.clixml'
$savedSettings = $null
if ([IO.File]::Exists($profileFile)) {
    $savedSettings = Import-Clixml -LiteralPath $profileFile
    if ($savedSettings.DjangoKey -isnot [Security.SecureString] -or $savedSettings.DjangoKey.Length -eq 0) {
        throw 'Ключ Django в локальном профиле повреждён.'
    }
}

$djangoKey = if ($savedSettings) {
    $savedSettings.DjangoKey
} elseif ($env:DJANGO_SECRET_KEY) {
    New-LocalSecureString $env:DJANGO_SECRET_KEY
} else {
    $generatedKey = & $localPython -c 'import secrets; print(secrets.token_urlsafe(48))'
    if ($LASTEXITCODE -ne 0 -or -not $generatedKey) {
        throw 'Не удалось создать ключ Django.'
    }
    New-LocalSecureString $generatedKey
}

$saveProfile = -not $savedSettings -or $ResetDatabasePassword
$databasePassword = if ($ResetDatabasePassword) {
    Read-Host 'Новый пароль PostgreSQL для postgres' -AsSecureString
} elseif ($savedSettings) {
    $savedSettings.DatabasePassword
} elseif ($env:DB_PASSWORD) {
    New-LocalSecureString $env:DB_PASSWORD
} else {
    Read-Host 'Пароль PostgreSQL для postgres' -AsSecureString
}
if ($databasePassword -isnot [Security.SecureString] -or $databasePassword.Length -eq 0) {
    throw 'Нужен пароль PostgreSQL. Для повторного ввода запусти .\start-local.ps1 -ResetDatabasePassword.'
}

$launchEnvironment = @{
    DJANGO_DEBUG = '1'
    DJANGO_SECRET_KEY = [Net.NetworkCredential]::new('', $djangoKey).Password
    DB_HOST = '127.0.0.1'
    DB_PORT = '5433'
    DB_NAME = 'online_cinema'
    DB_USER = 'postgres'
    DB_PASSWORD = [Net.NetworkCredential]::new('', $databasePassword).Password
}
$previousEnvironment = @{}

try {
    foreach ($entry in $launchEnvironment.GetEnumerator()) {
        $previousEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }

    if ($saveProfile) {
        & $localPython $manageScript inspect_database | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Не удалось проверить подключение к PostgreSQL. Введённый пароль не сохранён.'
        }

        [IO.Directory]::CreateDirectory($profileDirectory) | Out-Null
        $pendingFile = Join-Path $profileDirectory ("local-dev-{0}.tmp" -f [Guid]::NewGuid())
        try {
            [pscustomobject]@{
                DjangoKey = $djangoKey
                DatabasePassword = $databasePassword
            } | Export-Clixml -LiteralPath $pendingFile -Encoding utf8
            if ($savedSettings) {
                [IO.File]::Replace($pendingFile, $profileFile, [NullString]::Value)
            } else {
                [IO.File]::Move($pendingFile, $profileFile)
            }
        } finally {
            [IO.File]::Delete($pendingFile)
        }
        Write-Host 'Локальные настройки сохранены в зашифрованном виде.'
    }

    & $localPython $manageScript runserver 127.0.0.1:8000
    if ($LASTEXITCODE -ne 0) {
        throw 'Django не запустился. Причина указана выше. Если изменился пароль PostgreSQL, используй -ResetDatabasePassword.'
    }
} finally {
    foreach ($entry in $previousEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}
