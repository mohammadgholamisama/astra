# Creates PostgreSQL role + database for Astra.
# Run once:  powershell -ExecutionPolicy Bypass -File backend\scripts\init_db.ps1

$ErrorActionPreference = "Stop"
$psql = "C:\Program Files\PostgreSQL\17\bin\psql.exe"
if (-not (Test-Path $psql)) {
    $found = Get-ChildItem "C:\Program Files\PostgreSQL" -Recurse -Filter psql.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found) { throw "psql.exe not found. Install PostgreSQL first." }
    $psql = $found.FullName
}

$pgUser = Read-Host "PostgreSQL superuser (default: postgres)"
if ([string]::IsNullOrWhiteSpace($pgUser)) { $pgUser = "postgres" }

$secure = Read-Host "Password for '$pgUser'" -AsSecureString
$BSTR = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$env:PGPASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($BSTR)

Write-Host "Connecting..."
& $psql -U $pgUser -h 127.0.0.1 -p 5432 -d postgres -v ON_ERROR_STOP=1 -c "SELECT version();" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Login failed. Wrong password or PostgreSQL not running." }

$sql = @"
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'astra') THEN
    CREATE ROLE astra LOGIN PASSWORD 'astra_secret';
  ELSE
    ALTER ROLE astra WITH LOGIN PASSWORD 'astra_secret';
  END IF;
END
`$`$;

SELECT 'ok_role' AS status;

SELECT 1 FROM pg_database WHERE datname = 'astra';
"@

& $psql -U $pgUser -h 127.0.0.1 -p 5432 -d postgres -v ON_ERROR_STOP=1 -c $sql
if ($LASTEXITCODE -ne 0) { throw "Failed creating role." }

$dbExists = & $psql -U $pgUser -h 127.0.0.1 -p 5432 -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='astra'"
if ($dbExists -ne "1") {
    & $psql -U $pgUser -h 127.0.0.1 -p 5432 -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE astra OWNER astra;"
    if ($LASTEXITCODE -ne 0) { throw "Failed creating database." }
}

& $psql -U $pgUser -h 127.0.0.1 -p 5432 -d astra -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO astra; ALTER SCHEMA public OWNER TO astra;"

Write-Host ""
Write-Host "Done. DATABASE_URL should be:"
Write-Host "postgresql+asyncpg://astra:astra_secret@localhost:5432/astra"
Write-Host "Now restart backend: uvicorn app.main:app --reload --port 8000"
