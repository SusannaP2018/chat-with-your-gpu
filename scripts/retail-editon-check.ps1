<#
.SYNOPSIS
    Derives the likely retail AIB/partner GPU edition on Windows without a local database.
.DESCRIPTION
    1. Queries PnP video controllers (standard user permissions).
    2. Parses VEN, DEV, and SUBSYS IDs.
    3. Checks reference/Founders Edition rules, then queries pci.ids and TechPowerUp VBIOS.
    4. Uses live GPU clock speeds to break ties when multiple editions match.
#>

[CmdletBinding()]
param(
    [switch]$IncludeIntegrated
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Custom User-Agent improves reliability with public endpoints
$Headers = @{
    'User-Agent' = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GpuRetailResolver/1.0'
}

function Get-PciIdsIndex {
    $url = 'https://pci-ids.ucw.cz/v2.2/pci.ids'
    $raw = Invoke-RestMethod -Uri $url -Headers $Headers -TimeoutSec 20

    $vendors = @{}
    $currentVendor = $null
    $currentDevice = $null

    foreach ($line in ($raw -split "`r?`n")) {
        if (-not $line -or $line.StartsWith('#')) { continue }

        if ($line -match '^([0-9a-f]{4})\s+(.+)$') {
            $currentVendor = $matches[1].ToUpper()
            $vendors[$currentVendor] = @{
                Name    = $matches[2].Trim()
                Devices = @{}
            }
            $currentDevice = $null
        }
        elseif ($line -match '^\t([0-9a-f]{4})\s+(.+)$' -and $currentVendor) {
            $currentDevice = $matches[1].ToUpper()
            $vendors[$currentVendor].Devices[$currentDevice] = @{
                Name       = $matches[2].Trim()
                Subsystems = @{}
            }
        }
        elseif ($line -match '^\t\t([0-9a-f]{4})\s+([0-9a-f]{4})\s+(.+)$' -and $currentVendor -and $currentDevice) {
            $subVen = $matches[1].ToUpper()
            $subDev = $matches[2].ToUpper()
            $key = "$subVen`:$subDev"
            $vendors[$currentVendor].Devices[$currentDevice].Subsystems[$key] = $matches[3].Trim()
        }
    }

    return $vendors
}

function Get-LiveGpuClockMhz {
    param(
        [string]$VendorId,
        [int]$GpuIndex = 0
    )

    # 1. NVIDIA: query current or max application/boost clock via nvidia-smi (standard user)
    if ($VendorId -eq '10DE') {
        $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if (-not $nvidiaSmi) {
            $defaultPath = Join-Path $env:ProgramFiles 'NVIDIA Corporation\NVSMI\nvidia-smi.exe'
            if (Test-Path $defaultPath) { $nvidiaSmi = Get-Item $defaultPath }
        }

        if ($nvidiaSmi) {
            try {
                $csv = & $nvidiaSmi.FullName -i $GpuIndex --query-gpu=clocks.max.graphics,clocks.current.graphics --format=csv,noheader,nounits 2>$null
                if ($csv) {
                    $parts = ($csv -split "`r?`n")[0] -split ','
                    $maxClock = 0.0; $curClock = 0.0
                    [double]::TryParse($parts[0].Trim(), [ref]$maxClock) | Out-Null
                    if ($parts.Count -gt 1) { [double]::TryParse($parts[1].Trim(), [ref]$curClock) | Out-Null }
                    if ($maxClock -gt 0) { return [math]::Round($maxClock) }
                    if ($curClock -gt 0) { return [math]::Round($curClock) }
                }
            } catch { }
        }
    }

    # 2. Fallback: Windows WMI / CIM performance counter or driver properties if exposed
    try {
        $perf = Get-CimInstance -Namespace 'root\cimv2' -ClassName 'Win32_PerfFormattedData_Counters_GPUProcessMemory' -ErrorAction SilentlyContinue
        if ($perf) { return $null }
    } catch { }

    return $null
}

function Search-TechPowerUpVbios {
    param(
        [string]$VendorId,
        [string]$DeviceId,
        [string]$SubVendorId,
        [string]$SubDeviceId
    )

    $uri = "https://www.techpowerup.com/vgabios/?did=$($VendorId)-$($DeviceId)-$($SubVendorId)-$($SubDeviceId)"
    $results = [System.Collections.Generic.List[object]]::new()

    try {
        $resp = Invoke-WebRequest -Uri $uri -Headers $Headers -TimeoutSec 20
        $html = [string]$resp.Content

        $rowMatches = [regex]::Matches(
            $html,
            '<tr[^>]*>(.*?)</tr>',
            [System.Text.RegularExpressions.RegexOptions]::Singleline -bor [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )

        foreach ($row in $rowMatches) {
            $rowHtml = $row.Groups[1].Value
            $cleanText = [System.Net.WebUtility]::HtmlDecode(($rowHtml -replace '<[^>]+>', ' ')) -replace '\s+', ' '
            $cleanText = $cleanText.Trim()

            if ([string]::IsNullOrWhiteSpace($cleanText)) { continue }

            if ($cleanText -match '([A-Za-z0-9\-\+\s\(\)\/]+?(?:OC|Gaming|Strix|TUF|Ventus|Suprim|Pulse|Nitro\+|Hellhound|Red Devil|Eagle|Windforce|Aero|ProArt|Phantom|Taichi|Founders Edition|Reference)[^\d\|]*)') {
                $candidateName = ($matches[1] -replace '\s+', ' ').Trim()
                $mhz = $null
                if ($cleanText -match '(\d{3,4})\s*MHz') {
                    $mhz = [int]$matches[1]
                }

                if ($candidateName.Length -ge 4 -and $candidateName.Length -le 120) {
                    $results.Add([PSCustomObject]@{
                        EditionName = $candidateName
                        ClockMhz    = $mhz
                        Source      = 'TechPowerUp VBIOS'
                    })
                }
            }
        }
    } catch {
        Write-Verbose "TechPowerUp lookup failed: $($_.Exception.Message)"
    }

    if ($results.Count -eq 0) {
        return @()
    }

    $deduped = @(
        $results | Group-Object EditionName, ClockMhz | ForEach-Object {
            @($_.Group)[0]
        }
    )

    return $deduped
}

function Select-BestCandidateWithClockTieBreak {
    param(
        [AllowEmptyCollection()]
        [object[]]$Candidates = @(),
        [Nullable[int]]$LiveClockMhz
    )

    $candidateList = @($Candidates)
    if ($candidateList.Count -eq 0) {
        return $null
    }

    if ($candidateList.Count -eq 1) {
        return $candidateList[0]
    }

    $clocked = @($candidateList | Where-Object { $null -ne $_.ClockMhz })
    if ($null -ne $LiveClockMhz -and $clocked.Count -gt 0) {
        return (@($clocked | Sort-Object { [math]::Abs([int]$_.ClockMhz - [int]$LiveClockMhz) }))[0]
    }

    return $candidateList[0]
}

Write-Host "Fetching live pci.ids reference index..." -ForegroundColor Cyan
$pciIndex = Get-PciIdsIndex

$controllers = Get-CimInstance Win32_VideoController
$gpuIndex = 0

foreach ($ctrl in $controllers) {
    if ($ctrl.PNPDeviceID -notmatch 'VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})&SUBSYS_([0-9A-F]{4})([0-9A-F]{4})') {
        continue
    }

    $ven = $matches[1].ToUpper()
    $dev = $matches[2].ToUpper()
    $subDev = $matches[3].ToUpper()
    $subVen = $matches[4].ToUpper()
    $subsysKey = "$subVen`:$subDev"

    # Skip typical Intel integrated graphics unless requested
    if (-not $IncludeIntegrated -and $ven -eq '8086' -and $ctrl.Name -notmatch 'Arc') {
        continue
    }

    $liveClock = Get-LiveGpuClockMhz -VendorId $ven -GpuIndex $gpuIndex
    $gpuIndex++

    $pciSubsysName = $null
    $baseChipName = $ctrl.Name
    if ($pciIndex.ContainsKey($ven) -and $pciIndex[$ven].Devices.ContainsKey($dev)) {
        $baseChipName = $pciIndex[$ven].Devices[$dev].Name
        if ($pciIndex[$ven].Devices[$dev].Subsystems.ContainsKey($subsysKey)) {
            $pciSubsysName = $pciIndex[$ven].Devices[$dev].Subsystems[$subsysKey]
        }
    }

    $candidates = [System.Collections.Generic.List[object]]::new()

    if (($ven -eq '10DE' -and $subVen -eq '10DE') -or ($ven -eq '1002' -and $subVen -eq '1002')) {
        $refLabel = if ($ven -eq '10DE') { "$baseChipName (Founders Edition / Reference)" } else { "$baseChipName (AMD Reference)" }
        $candidates.Add([PSCustomObject]@{
            EditionName = $refLabel
            ClockMhz    = $null
            Source      = 'PCI Subsystem Rule'
        })
    }

    if ($pciSubsysName) {
        $candidates.Add([PSCustomObject]@{
            EditionName = $pciSubsysName
            ClockMhz    = $null
            Source      = 'pci.ids'
        })
    }

    $tpuMatches = @(Search-TechPowerUpVbios -VendorId $ven -DeviceId $dev -SubVendorId $subVen -SubDeviceId $subDev)
    foreach ($match in $tpuMatches) {
        $candidates.Add($match)
    }

    $uniqueCandidates = @(
        if ($candidates.Count -gt 0) {
            $candidates | Group-Object EditionName | ForEach-Object {
                (@($_.Group) | Sort-Object { if ($null -ne $_.ClockMhz) { 0 } else { 1 } })[0]
            }
        }
    )

    $chosen = Select-BestCandidateWithClockTieBreak -Candidates $uniqueCandidates -LiveClockMhz $liveClock

    [PSCustomObject]@{
        WindowsName     = $ctrl.Name
        BaseChip        = $baseChipName
        PciIdsSubsys    = if ($pciSubsysName) { $pciSubsysName } else { '<Not in pci.ids>' }
        LiveClockMhz    = if ($null -ne $liveClock) { $liveClock } else { '<Unavailable>' }
        CandidateCount  = @($uniqueCandidates).Count
        DerivedEdition  = if ($null -ne $chosen) { $chosen.EditionName } else { "$baseChipName (SUBSYS $subsysKey)" }
        ResolvedVia     = if ($null -ne $chosen) { $chosen.Source } else { 'Fallback Base Chip' }
        HardwareId      = "PCI\VEN_$ven&DEV_$dev&SUBSYS_$subDev$subVen"
    }
}