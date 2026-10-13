param(
  [Parameter(Mandatory=$true)][int]$BrowserPid,
  [ValidateSet('fill','dump','click')][string]$Mode = 'dump',
  [string]$FolderPath = '',
  [int]$TimeoutSec = 20
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$Cond = [System.Windows.Automation.Condition]::TrueCondition

function Get-PidSet {
  $all = Get-CimInstance Win32_Process -Filter "Name='chrome.exe' or Name='msedge.exe'"
  $set = New-Object 'System.Collections.Generic.HashSet[int]'
  [void]$set.Add($BrowserPid)
  $changed = $true
  while ($changed) {
    $changed = $false
    foreach ($p in $all) { if ($set.Contains([int]$p.ParentProcessId) -and -not $set.Contains([int]$p.ProcessId)) { [void]$set.Add([int]$p.ProcessId); $changed = $true } }
  }
  return $set
}

function Get-MyTopWindows {
  $set = Get-PidSet
  $root = $AE::RootElement
  $wins = $root.FindAll($TS::Children, $Cond)
  $out = @()
  foreach ($w in $wins) { try { if ($set.Contains($w.Current.ProcessId)) { $out += $w } } catch {} }
  return $out
}

if ($Mode -eq 'click') {
  $bc = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
  foreach ($w in (Get-MyTopWindows)) {
    foreach ($b in $w.FindAll($TS::Descendants, $bc)) {
      if ($b.Current.Name -eq $FolderPath) {
        Write-Output "Invoking '$($b.Current.Name)'"
        $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        Start-Sleep -Seconds 2
        exit 0
      }
    }
  }
  Write-Output "button '$FolderPath' not found"; exit 5
}

if ($Mode -eq 'dump') {
  $wins = Get-MyTopWindows
  Write-Output "Top-level windows owned by browser pid tree: $($wins.Count)"
  foreach ($w in $wins) {
    Write-Output "WINDOW name='$($w.Current.Name)' class='$($w.Current.ClassName)' pid=$($w.Current.ProcessId)"
    $desc = $w.FindAll($TS::Descendants, $Cond)
    $n = 0
    foreach ($d in $desc) {
      $nm = $d.Current.Name
      $ct = $d.Current.ControlType.ProgrammaticName
      if ($nm -and ($ct -match 'Text|Button|Pane|Group|Window|Dialog|Alert|ToolBar') -and $nm.Length -lt 300) {
        if ($nm -match 'developer|Developer|extension|Extension|Turn off|Keep|unsupported|flag|automated|Disable|SITE|stability|security|debug|controlled|store|Allow|started') {
          Write-Output "   [$ct] '$nm'"; $n++
        }
      }
      if ($n -gt 40) { break }
    }
  }
  exit 0
}

# fill: find the folder picker dialog and select $FolderPath
$deadline = (Get-Date).AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
  foreach ($w in (Get-MyTopWindows)) {
    $dlg = $null
    if ($w.Current.ClassName -eq '#32770') { $dlg = $w }
    else {
      $c = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, '#32770')
      $dlg = $w.FindFirst($TS::Descendants, $c)
    }
    if ($dlg) {
      Write-Output "Found dialog '$($dlg.Current.Name)' pid=$($dlg.Current.ProcessId)"
      $target = $null
      $all = $dlg.FindAll($TS::Descendants, $Cond)
      foreach ($e in $all) {
        $aid = $e.Current.AutomationId; $nm = $e.Current.Name; $ct = $e.Current.ControlType.ProgrammaticName
        if ($aid -eq '1152' -or ($nm -match '^Folder' -and $ct -match 'Edit|ComboBox')) {
          Write-Output "  candidate [$ct] name='$nm' aid='$aid'"
          try { [void]$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern); if (-not $target -or $ct -match 'Edit') { $target = $e } } catch {}
        }
      }
      if (-not $target) {
        # Fall back to Win32: the 1152 control is a ComboBoxEx32 hosting an Edit; WM_SETTEXT it directly.
        $pane = $null
        foreach ($e in $all) { if ($e.Current.AutomationId -eq '1152') { $pane = $e; break } }
        if (-not $pane) { Write-Output 'no Folder control'; exit 3 }
        Add-Type -Namespace W -Name U -MemberDefinition @'
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern System.IntPtr FindWindowEx(System.IntPtr parent, System.IntPtr after, string cls, string title);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern System.IntPtr SendMessage(System.IntPtr h, int msg, System.IntPtr w, string l);
'@
        $h = [System.IntPtr]$pane.Current.NativeWindowHandle
        Write-Output "  1152 hwnd=$h class=$($pane.Current.ClassName)"
        $edit = $h
        $cb = [W.U]::FindWindowEx($h, [System.IntPtr]::Zero, 'ComboBox', $null)
        if ($cb -ne [System.IntPtr]::Zero) { $e2 = [W.U]::FindWindowEx($cb, [System.IntPtr]::Zero, 'Edit', $null); if ($e2 -ne [System.IntPtr]::Zero) { $edit = $e2 } }
        else { $e2 = [W.U]::FindWindowEx($h, [System.IntPtr]::Zero, 'Edit', $null); if ($e2 -ne [System.IntPtr]::Zero) { $edit = $e2 } }
        Write-Output "  edit hwnd=$edit"
        [void][W.U]::SendMessage($edit, 0x000C, [System.IntPtr]::Zero, $FolderPath)  # WM_SETTEXT
      } else {
        $vp = $target.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $vp.SetValue($FolderPath)
      }
      Start-Sleep -Milliseconds 500
      $btn = $null
      $bc = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
      foreach ($b in $dlg.FindAll($TS::Descendants, $bc)) { if ($b.Current.Name -match '^Select Folder$|^Select folder$' -or ($b.Current.AutomationId -eq '1' -and $b.Current.ClassName -eq 'Button')) { $btn = $b; break } }
      if (-not $btn) {
        $dh = [System.IntPtr]$dlg.Current.NativeWindowHandle
        Write-Output "  no UIA button; sending WM_COMMAND IDOK to dialog hwnd=$dh"
        Add-Type -Namespace W2 -Name U2 -MemberDefinition @'
[DllImport("user32.dll")] public static extern System.IntPtr SendMessage(System.IntPtr h, int msg, System.IntPtr w, System.IntPtr l);
'@
        [void][W2.U2]::SendMessage($dh, 0x0111, [System.IntPtr]1, [System.IntPtr]::Zero)  # WM_COMMAND, IDOK (navigates into folder)
        Start-Sleep -Milliseconds 1500
        Write-Output "  second IDOK (select current folder)"
        [void][W2.U2]::SendMessage($dh, 0x0111, [System.IntPtr]1, [System.IntPtr]::Zero)
        Start-Sleep -Seconds 1
        exit 0
      }
      Write-Output "Invoking button '$($btn.Current.Name)'"
      $ip = $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
      $ip.Invoke()
      Start-Sleep -Seconds 1
      exit 0
    }
  }
  Start-Sleep -Milliseconds 500
}
Write-Output 'dialog not found'
exit 2
