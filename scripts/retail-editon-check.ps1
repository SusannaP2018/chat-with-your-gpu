<#
.SYNOPSIS
    Derives the likely retail AIB/partner GPU edition on Windows without a local database.
#>
[CmdletBinding()]
param(
    [switch]$IncludeIntegrated
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Script:Headers = @{
    'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GpuRetailResolver/1.0'
}

function Get-PciIdsIndex {
    $raw = Invoke-RestMethod -Uri 'https://pci-ids.ucw.cz/v2.2/pci.ids' -Headers $Script:Headers -TimeoutSec 20
    $vendors = @{}
    $vendor = $null
    $device = $null

    foreach ($line in ($raw -split "`r?`n")) {
        if (-not $line -or $line[0] -eq '#') { continue }

        if ($line -match '^([0-9a-f]{4})\s+(.+)$') {
            $vendor = $matches[1].ToUpperInvariant()
            $device = $null
            $vendors[$vendor] = @{ Name = $matches[2].Trim(); Devices = @{} }
        }
        elseif ($vendor -and $line -match '^\t([0-9a-f]{4})\s+(.+)$') {
            $device = $matches[1].ToUpperInvariant()
            $vendors[$vendor].Devices[$device] = @{ Name = $matches[2].Trim(); Subsystems = @{} }
        }
        elseif ($device -and $line -match '^\t\t([0-9a-f]{4})\s+([0-9a-f]{4})\s+(.+)$') {
            $subKey = "$($matches[1].ToUpperInvariant()):$($matches[2].ToUpperInvariant())"
            $vendors[$vendor].Devices[$device].Subsystems[$subKey] = $matches[3].Trim()
        }
    }

    return $vendors
}

function Get-NvidiaClocksByIndex {
    $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $smi) {
        $fallback = Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'
        if (Test-Path -LiteralPath $fallback) { $smi = Get-Item -LiteralPath $fallback }
    }
    if (-not $smi) { return @{} }

    $map = @{}
    try {
        $lines = & $smi.FullName --query-gpu=index,clocks.max.graphics,clocks.current.graphics --format=csv,noheader,nounits 2>$null
        foreach ($line in @($lines)) {
            $cols = $line -split '\s*,\s*'
            if ($cols.Count -lt 2) { continue }

            $idx = 0; $maxClk = 0.0; $curClk = 0.0
            if (-not [int]::TryParse($cols[0], [ref]$idx)) { continue }
            [double]::TryParse($cols[1], [ref]$maxClk) | Out-Null
            if ($cols.Count -gt 2) { [double]::TryParse($cols[2], [ref]$curClk) | Out-Null }

            $best = if ($maxClk -gt 0) { [int][math]::Round($maxClk) }
                    elseif ($curClk -gt 0) { [int][math]::Round($curClk) }
                    else { $null }

            if ($null -ne $best) { $map[$idx] = $best }
        }
    }
    catch {
        Write-Verbose "nvidia-smi query failed: $($_.Exception.Message)"
    }

    return $map
}

function Search-TechPowerUpVbios {
    param(
        [string]$VendorId,
        [string]$DeviceId,
        [string]$SubVendorId,
        [string]$SubDeviceId
    )

    $uri = "https://www.techpowerup.com/vgabios/?did=$VendorId-$DeviceId-$SubVendorId-$SubDeviceId"
    $editionPattern = '([A-Za-z0-9\-\+\s\(\)\/]+?(?:OC|Gaming|Strix|TUF|Ventus|Suprim|Pulse|Nitro\+|Hellhound|Red Devil|Eagle|Windforce|Aero|ProArt|Phantom|Taichi|Founders Edition|Reference)[^\d\|]*)'
    $seen = @{}

    try {
        $resp = Invoke-WebRequest -Uri $uri -Headers $Script:Headers -TimeoutSec 20
        $rows = [regex]::Matches(
            [string]$resp.Content,
            '<tr[^>]*>(.*?)</tr>',
            [System.Text.RegularExpressions.RegexOptions]::Singleline -bor [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )

        foreach ($row in $rows) {
            $text = [System.Net.WebUtility]::HtmlDecode(($row.Groups[1].Value -replace '<[^>]+>', ' ')) -replace '\s+', ' '
            $text = $text.Trim()
            if (-not $text -or $text -notmatch $editionPattern) { continue }

            $name = ($matches[1] -replace '\s+', ' ').Trim()
            if ($name.Length -lt 4 -or $name.Length -gt 120) { continue }

            $mhz = if ($text -match '(\d{3,4})\s*MHz') { [int]$matches[1] } else { $null }
            $dedupKey = "$name|$mhz"

            if (-not $seen.ContainsKey($dedupKey)) {
                $seen[$dedupKey] = [PSCustomObject]@{
                    EditionName = $name
                    ClockMhz    = $mhz
                    Source      = 'TechPowerUp VBIOS'
                }
            }
        }
    }
    catch {
        Write-Verbose "TechPowerUp lookup failed: $($_.Exception.Message)"
    }

    return @($seen.Values)
}

function Select-BestCandidate {
    param(
        [AllowEmptyCollection()][object[]]$Candidates = @(),
        [Nullable[int]]$LiveClockMhz
    )

    $list = @($Candidates)
    if ($list.Count -eq 0) { return $null }

    if ($null -ne $LiveClockMhz) {
        $clocked = @($list | Where-Object { $null -ne $_.ClockMhz })
        if ($clocked.Count -gt 0) {
            return (@($clocked | Sort-Object { [math]::Abs([int]$_.ClockMhz - $LiveClockMhz) }))[0]
        }
    }

    return $list[0]
}

Write-Verbose 'Fetching live pci.ids reference index...'
$pciIndex = Get-PciIdsIndex
$nvidiaClocks = Get-NvidiaClocksByIndex
$nvOrdinal = 0

foreach ($ctrl in (Get-CimInstance -ClassName Win32_VideoController)) {
    if ($ctrl.PNPDeviceID -notmatch 'VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})&SUBSYS_([0-9A-F]{4})([0-9A-F]{4})') {
        continue
    }

    $ven = $matches[1].ToUpperInvariant()
    $dev = $matches[2].ToUpperInvariant()
    $subDev = $matches[3].ToUpperInvariant()
    $subVen = $matches[4].ToUpperInvariant()
    $subKey = "$subVen`:$subDev"

    if (-not $IncludeIntegrated -and $ven -eq '8086' -and $ctrl.Name -notmatch 'Arc') {
        continue
    }

    $liveClock = $null
    if ($ven -eq '10DE') {
        if ($nvidiaClocks.ContainsKey($nvOrdinal)) { $liveClock = $nvidiaClocks[$nvOrdinal] }
        $nvOrdinal++
    }

    $baseChipName = $ctrl.Name
    $pciSubsysName = $null
    $devEntry = if ($pciIndex.ContainsKey($ven) -and $pciIndex[$ven].Devices.ContainsKey($dev)) {
        $pciIndex[$ven].Devices[$dev]
    } else { $null }

    if ($devEntry) {
        $baseChipName = $devEntry.Name
        if ($devEntry.Subsystems.ContainsKey($subKey)) {
            $pciSubsysName = $devEntry.Subsystems[$subKey]
        }
    }

    $byEdition = @{}
    $addCandidate = {
        param($Name, $ClockMhz, $Source)
        if (-not $Name) { return }
        if (-not $byEdition.ContainsKey($Name)) {
            $byEdition[$Name] = [PSCustomObject]@{
                EditionName = $Name
                ClockMhz    = $ClockMhz
                Source      = $Source
            }
        }
        elseif ($null -eq $byEdition[$Name].ClockMhz -and $null -ne $ClockMhz) {
            $byEdition[$Name].ClockMhz = $ClockMhz
        }
    }

    if ($ven -eq '10DE' -and $subVen -eq '10DE') {
        & $addCandidate "$baseChipName (Founders Edition / Reference)" $null 'PCI Subsystem Rule'
    }
    elseif ($ven -eq '1002' -and $subVen -eq '1002') {
        & $addCandidate "$baseChipName (AMD Reference)" $null 'PCI Subsystem Rule'
    }

    if ($pciSubsysName) {
        & $addCandidate $pciSubsysName $null 'pci.ids'
    }

    foreach ($tpu in (Search-TechPowerUpVbios -VendorId $ven -DeviceId $dev -SubVendorId $subVen -SubDeviceId $subDev)) {
        & $addCandidate $tpu.EditionName $tpu.ClockMhz $tpu.Source
    }

    $uniqueCandidates = @($byEdition.Values)
    $chosen = Select-BestCandidate -Candidates $uniqueCandidates -LiveClockMhz $liveClock

    [PSCustomObject]@{
        WindowsName    = $ctrl.Name
        BaseChip       = $baseChipName
        PciIdsSubsys   = $pciSubsysName
        LiveClockMhz   = $liveClock
        CandidateCount = $uniqueCandidates.Count
        DerivedEdition = if ($chosen) { $chosen.EditionName } else { "$baseChipName (SUBSYS $subKey)" }
        ResolvedVia    = if ($chosen) { $chosen.Source } else { 'Fallback Base Chip' }
        HardwareId     = "PCI\VEN_$ven&DEV_$dev&SUBSYS_$subDev$subVen"
    }
}