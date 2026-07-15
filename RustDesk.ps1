$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'

$Server='100.118.93.35'
$Key='Oy07x28pfAmrarWqvySY+xuKNbvUdU7xTfnze25koMo='
$PeerAddress='100.118.93.35'

$Dir=Join-Path $env:TEMP 'RustDesk-OneClick'
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

$Release=Invoke-RestMethod 'https://api.github.com/repos/rustdesk/rustdesk/releases/latest' -Headers @{ 'User-Agent'='RustDesk-OneClick' }
$Asset=$Release.assets | Where-Object { $_.name -match 'x86_64\.exe$' -and $_.name -notmatch 'sciter|aarch64' } | Select-Object -First 1
if(-not $Asset){ throw 'RustDesk Windows x64 installer not found.' }

$Installer=Join-Path $Dir 'rustdesk.exe'
Invoke-WebRequest $Asset.browser_download_url -OutFile $Installer -Headers @{ 'User-Agent'='RustDesk-OneClick' }

$p=Start-Process $Installer -ArgumentList '--silent-install' -PassThru
if(-not $p.WaitForExit(180000)){ Stop-Process $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep 10

$Exe=@(
  (Join-Path $env:ProgramFiles 'RustDesk\rustdesk.exe'),
  (Join-Path ${env:ProgramFiles(x86)} 'RustDesk\rustdesk.exe'),
  (Join-Path $env:LOCALAPPDATA 'rustdesk\rustdesk.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if(-not $Exe){ throw 'RustDesk installation failed.' }

$p=Start-Process $Exe -ArgumentList '--install-service' -PassThru
$p.WaitForExit(60000) | Out-Null
Start-Sleep 5

$Json=[ordered]@{
  host=$Server
  relay=($Server+':21117')
  api=''
  key=$Key
} | ConvertTo-Json -Compress

$b64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Json)).TrimEnd('=').Replace('+','-').Replace('/','_')
$chars=$b64.ToCharArray()
[array]::Reverse($chars)
$Config=-join $chars

$p=Start-Process $Exe -ArgumentList @('--config',$Config) -PassThru
$p.WaitForExit(60000) | Out-Null

$Secure=Read-Host 'Enter RustDesk permanent password' -AsSecureString
$bstr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try { $Password=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if($Password){ & $Exe --password $Password *> $null }

$Service=Get-Service -Name RustDesk -ErrorAction SilentlyContinue
if($Service -and $Service.Status -ne 'Running'){ Start-Service RustDesk }

$Checks=21116,21117,21118 | ForEach-Object {
  [pscustomobject]@{
    Port=$_
    Tcp=(Test-NetConnection $Server -Port $_ -InformationLevel Quiet)
  }
}
$Checks | Format-Table -AutoSize

$Ts=Get-Command tailscale.exe -ErrorAction SilentlyContinue
if($Ts){
  Write-Host "`nTailscale path test:"
  & $Ts.Source ping $Server
}else{
  Write-Warning 'Tailscale was not found. Install and log in to Tailscale first.'
}

$Id=''
try {
  $Id=& $Exe --get-id 2>$null | Select-Object -First 1
  if($Id){ $Id=$Id.ToString().Trim() }
}catch{}

Write-Host "`nRustDesk ID: $Id"
Write-Host 'Opening direct RustDesk connection test...'
Start-Process $Exe -ArgumentList @('--connect',$PeerAddress)