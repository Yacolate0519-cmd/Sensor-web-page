# Read-only dump of every channel listed in the manual. Only 6112/6122/6142/6152 (read byte/int/float/dword).
param([string]$Out = "$PSScriptRoot\read_all_result.txt")
$allowed = 0x6112, 0x6122, 0x6142, 0x6152
function Frame([int]$cmd, [int]$ch) {
  if ($allowed -notcontains $cmd) { throw "blocked command $cmd" }
  $b = [byte[]](0x0C,0xF3,0xFF,0xFF,0x00,0x00,(($cmd -shr 8) -band 0xFF),($cmd -band 0xFF),(($ch -shr 8) -band 0xFF),($ch -band 0xFF),0,0)
  $sum = 0; for ($i = 2; $i -lt 10; $i++) { $sum += $b[$i] }
  $b[10] = ($sum -shr 8) -band 0xFF; $b[11] = $sum -band 0xFF
  return ,$b
}
$set = [ordered]@{
  0x6112 = 18,19,20,28,33,120,140,200,202,209,252,261,265,451,454,650,652,653,654,655,803,1006,1007,1200,1512
  0x6122 = 3,5,235,236,237,238,239,240,241,242,270,275,276,1100,21000,21001,21002
  0x6142 = 205,207,208,233,234,250,251,256,282,283,450,453,650,651,652,653,906,907,920,921,930,931,932,933,936,937,938,939,940,941,942,943,944,945,946,947,966,967,968,976,977,978,980,981,983,984,51130,51131,51140,51141,65455,65461
  0x6152 = 100,900,901
}
$sp = New-Object System.IO.Ports.SerialPort 'COM8', 115200, 'None', 8, 'One'
$sp.DtrEnable = $false; $sp.RtsEnable = $false; $sp.ReadTimeout = 500; $sp.WriteTimeout = 500
$sp.Open()
$lines = New-Object System.Collections.Generic.List[string]
try {
  foreach ($kv in $set.GetEnumerator()) {
    foreach ($ch in $kv.Value) {
      $f = Frame $kv.Key $ch; $sp.DiscardInBuffer(); $sp.Write($f, 0, $f.Length)
      $sw = [Diagnostics.Stopwatch]::StartNew(); $buf = New-Object System.Collections.Generic.List[byte]
      while ($sw.ElapsedMilliseconds -lt 400) {
        while ($sp.BytesToRead -gt 0) { $buf.Add([byte]$sp.ReadByte()) }
        if ($buf.Count -ge 1 -and $buf.Count -ge $buf[0]) { break }
        Start-Sleep -Milliseconds 2
      }
      $b = $buf.ToArray(); $hex = ($b | ForEach-Object { $_.ToString('X2') }) -join ' '
      $ack = if ($b.Length -ge 8) { '{0:X2}{1:X2}' -f $b[6], $b[7] } else { '----' }
      $val = ''
      if ($ack -eq '4000') {
        $d = $b[12..($b.Length - 3)]
        switch ($kv.Key) {
          0x6142 { $val = [BitConverter]::ToSingle([byte[]]$d, 0).ToString('G7') }
          0x6112 { $val = "$($d[0]) (bits " + [Convert]::ToString($d[0], 2).PadLeft(8, '0') + ')' }
          default { $le = 0; for ($i = $d.Length - 1; $i -ge 0; $i--) { $le = $le * 256 + $d[$i] }; $be = 0; foreach ($x in $d) { $be = $be * 256 + $x }; $val = "LE=$le BE=$be" }
        }
      }
      $lines.Add(('{0:X4} {1,6} ack={2} val={3,-28} {4}' -f $kv.Key, $ch, $ack, $val, $hex))
    }
  }
} finally { $sp.Close(); $sp.Dispose() }
$lines | Set-Content -Encoding utf8 $Out
$lines
