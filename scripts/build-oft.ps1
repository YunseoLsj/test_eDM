[CmdletBinding()]
param(
    [string]$Folder = ".",
    [string]$HtmlPath,
    [string]$Repository = $env:GITHUB_REPOSITORY,
    [string]$RefName = $env:GITHUB_REF_NAME,
    [string]$CommitSha = $env:GITHUB_SHA,
    [string]$Subject,
    [switch]$RewriteRelativeImageSources
)

$ErrorActionPreference = "Stop"

function Get-FullPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Path))
}

function Get-RepoRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $root = [System.IO.Path]::GetFullPath((Get-Location).Path)
    if (-not $root.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $root = $root + [System.IO.Path]::DirectorySeparatorChar
    }

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the repository checkout: $Path"
    }

    return $fullPath.Substring($root.Length).Replace("\", "/")
}

function ConvertTo-UrlPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $segments = $Path.Replace("\", "/") -split "/"
    $encoded = foreach ($segment in $segments) {
        [System.Uri]::EscapeDataString($segment)
    }

    return ($encoded -join "/")
}

function ConvertTo-RawGitHubUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Ref,
        [Parameter(Mandatory = $true)][string]$RepoRelativePath
    )

    $encodedRef = ConvertTo-UrlPath -Path $Ref
    $encodedPath = ConvertTo-UrlPath -Path $RepoRelativePath

    return "https://raw.githubusercontent.com/$Repository/$encodedRef/$encodedPath"
}

function Test-IsExternalOrSpecialSource {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $true
    }

    $trimmed = $Value.Trim()
    if ($trimmed.StartsWith("//") -or $trimmed.StartsWith("/") -or $trimmed.StartsWith("#")) {
        return $true
    }

    $uri = $null
    if ([System.Uri]::TryCreate($trimmed, [System.UriKind]::Absolute, [ref]$uri)) {
        return $true
    }

    return $false
}

function Split-ResourceValue {
    param([Parameter(Mandatory = $true)][string]$Value)

    $firstMarker = -1
    foreach ($marker in @("?", "#")) {
        $index = $Value.IndexOf($marker)
        if ($index -ge 0 -and ($firstMarker -lt 0 -or $index -lt $firstMarker)) {
            $firstMarker = $index
        }
    }

    if ($firstMarker -ge 0) {
        return @{
            Path = $Value.Substring(0, $firstMarker)
            Suffix = $Value.Substring($firstMarker)
        }
    }

    return @{
        Path = $Value
        Suffix = ""
    }
}

function Rewrite-ImageSources {
    param(
        [Parameter(Mandatory = $true)][string]$Html,
        [Parameter(Mandatory = $true)][string]$HtmlDirectory,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Ref
    )

    $script:RewriteEvents = @()
    $pattern = [regex]'(?is)(<img\b[^>]*?\bsrc\s*=\s*)(["''])(.*?)(\2)'
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator]{
        param([System.Text.RegularExpressions.Match]$match)

        $source = $match.Groups[3].Value.Trim()
        if (Test-IsExternalOrSpecialSource -Value $source) {
            return $match.Value
        }

        $split = Split-ResourceValue -Value $source
        $resourcePath = $split["Path"]
        if ([string]::IsNullOrWhiteSpace($resourcePath)) {
            return $match.Value
        }

        $decodedResourcePath = [System.Uri]::UnescapeDataString($resourcePath)
        $resourceFullPath = [System.IO.Path]::GetFullPath((Join-Path $HtmlDirectory $decodedResourcePath))
        $resourceExists = Test-Path -LiteralPath $resourceFullPath -PathType Leaf
        if (-not $resourceExists) {
            throw "Relative image source was not found: $source"
        }

        $repoRelativePath = Get-RepoRelativePath -Path $resourceFullPath
        $rawUrl = ConvertTo-RawGitHubUrl -Repository $Repository -Ref $Ref -RepoRelativePath $repoRelativePath
        $newSource = $rawUrl + $split["Suffix"]

        $script:RewriteEvents += [ordered]@{
            from = $source
            to = $newSource
            repo_path = $repoRelativePath
            exists_in_checkout = $resourceExists
        }

        return $match.Groups[1].Value + $match.Groups[2].Value + $newSource + $match.Groups[2].Value
    }

    return $pattern.Replace($Html, $evaluator)
}

function Get-HtmlFiles {
    param(
        [string]$Folder,
        [string]$HtmlPath
    )

    if (-not [string]::IsNullOrWhiteSpace($HtmlPath)) {
        $fullHtmlPath = Get-FullPath -Path $HtmlPath
        if (-not (Test-Path -LiteralPath $fullHtmlPath -PathType Leaf)) {
            throw "HTML file not found: $HtmlPath"
        }

        return @(Get-Item -LiteralPath $fullHtmlPath)
    }

    $fullFolder = Get-FullPath -Path $Folder
    if (-not (Test-Path -LiteralPath $fullFolder -PathType Container)) {
        throw "Folder not found: $Folder"
    }

    $allFiles = @()
    $allFiles += Get-ChildItem -LiteralPath $fullFolder -File -Filter "*.html" -Recurse
    $allFiles += Get-ChildItem -LiteralPath $fullFolder -File -Filter "*.htm" -Recurse
    $allFiles = @($allFiles | Where-Object { $_.Name -notmatch "_oft_(input|build)\.html?$" } | Sort-Object FullName -Unique)

    if ($allFiles.Count -eq 0) {
        throw "No .html or .htm files found in $Folder"
    }

    $preferred = @($allFiles | Where-Object { $_.Name -notmatch "_local\.html?$" -and $_.Name -notmatch "^local\.html?$" })
    if ($preferred.Count -gt 0) {
        return $preferred
    }

    return $allFiles
}

function Get-MessageSubject {
    param(
        [Parameter(Mandatory = $true)][string]$Html,
        [Parameter(Mandatory = $true)][string]$Fallback
    )

    if (-not [string]::IsNullOrWhiteSpace($Subject)) {
        return $Subject
    }

    if ($Html -match "(?is)<title[^>]*>(.*?)</title>") {
        $title = [System.Net.WebUtility]::HtmlDecode($matches[1]).Trim()
        if (-not [string]::IsNullOrWhiteSpace($title)) {
            return $title
        }
    }

    return $Fallback
}

function Test-OftFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "OFT was not created: $Path"
    }

    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt 512) {
        throw "OFT is too small to be a valid Outlook template: $Path"
    }

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $header = New-Object byte[] 8
        $read = $stream.Read($header, 0, 8)
        if ($read -ne 8) {
            throw "Could not read OFT header: $Path"
        }
    }
    finally {
        $stream.Dispose()
    }

    $expected = [byte[]](0xD0, 0xCF, 0x11, 0xE0, 0xA1, 0xB1, 0x1A, 0xE1)
    for ($i = 0; $i -lt $expected.Length; $i++) {
        if ($header[$i] -ne $expected[$i]) {
            throw "OFT header is not a Compound File Binary Outlook item: $Path"
        }
    }
}

function Release-ComObject {
    param($ComObject)

    if ($null -ne $ComObject -and [System.Runtime.InteropServices.Marshal]::IsComObject($ComObject)) {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($ComObject)
    }
}

function New-OutlookApplication {
    try {
        return New-Object -ComObject Outlook.Application
    }
    catch {
        throw "Outlook desktop COM is unavailable. Run this on a Windows self-hosted GitHub Actions runner with Microsoft Outlook installed and a configured mail profile. $($_.Exception.Message)"
    }
}

$htmlFiles = @(Get-HtmlFiles -Folder $Folder -HtmlPath $HtmlPath)
$rawRef = $RefName
if (-not [string]::IsNullOrWhiteSpace($CommitSha)) {
    $rawRef = $CommitSha
}

if ($RewriteRelativeImageSources -and [string]::IsNullOrWhiteSpace($Repository)) {
    throw "-RewriteRelativeImageSources requires -Repository or GITHUB_REPOSITORY."
}

$outlook = New-OutlookApplication

try {
    try {
        $outlook.Session.Logon($null, $null, $false, $false)
    }
    catch {
        Write-Warning "Outlook profile logon was not explicit; continuing with the default profile. $($_.Exception.Message)"
    }

    foreach ($htmlFile in $htmlFiles) {
        $script:RewriteEvents = @()
        $html = [System.IO.File]::ReadAllText($htmlFile.FullName)
        $htmlDirectory = Split-Path -Parent $htmlFile.FullName
        $htmlForOutlook = $html

        if ($RewriteRelativeImageSources) {
            $htmlForOutlook = Rewrite-ImageSources -Html $html -HtmlDirectory $htmlDirectory -Repository $Repository -Ref $rawRef
        }

        $messageSubject = Get-MessageSubject -Html $html -Fallback $htmlFile.BaseName
        $oftPath = Join-Path $htmlFile.DirectoryName ($htmlFile.BaseName + ".oft")
        $buildJsonPath = Join-Path $htmlFile.DirectoryName ($htmlFile.BaseName + "_oft_build.json")
        if (Test-Path -LiteralPath $oftPath) {
            Remove-Item -LiteralPath $oftPath -Force
        }

        $mail = $null
        try {
            $mail = $outlook.CreateItem(0)
            $mail.BodyFormat = 2
            $mail.Subject = $messageSubject
            $mail.HTMLBody = $htmlForOutlook
            $mail.SaveAs($oftPath, 2)
        }
        finally {
            Release-ComObject -ComObject $mail
        }

        Test-OftFile -Path $oftPath

        $build = [ordered]@{
            generated_at = (Get-Date).ToUniversalTime().ToString("o")
            html = Get-RepoRelativePath -Path $htmlFile.FullName
            oft = Get-RepoRelativePath -Path $oftPath
            subject = $messageSubject
            outlook_version = $outlook.Version
            body_format = "HTML"
            relative_image_sources_rewritten = [bool]$RewriteRelativeImageSources
            rewrite_count = $script:RewriteEvents.Count
            rewrites = $script:RewriteEvents
        }

        $build | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $buildJsonPath -Encoding UTF8
        Write-Host "Generated OFT: $(Get-RepoRelativePath -Path $oftPath)"
    }
}
finally {
    Release-ComObject -ComObject $outlook
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
