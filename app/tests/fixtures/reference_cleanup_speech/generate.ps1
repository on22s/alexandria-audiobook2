param([string]$OutputDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
[void](New-Item -ItemType Directory -Path $OutputDirectory -Force)
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synthesizer.SelectVoice('Microsoft David Desktop')
    $synthesizer.Rate = 0
    $synthesizer.Volume = 70
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $phrases = @(
        @{file = 'original.wav'; transcript = 'The red lantern shines beside the quiet river today.'},
        @{file = 'changed-word.wav'; transcript = 'The blue lantern shines beside the quiet river today.'}
    )
    $records = @()
    foreach ($phrase in $phrases) {
        $target = Join-Path $OutputDirectory $phrase.file
        $synthesizer.SetOutputToWaveFile($target, $format)
        $synthesizer.Speak($phrase.transcript)
        $synthesizer.SetOutputToNull()
        $records += @{file = $phrase.file; requested_transcript = $phrase.transcript; sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLower()}
    }
    $manifest = @{generator = 'generate.ps1'; source = 'Locally generated synthetic test speech, not a private or recorded human voice'; voice = $synthesizer.Voice.Name; sample_rate = 24000; subtype = 'PCM_16'; changed_word = @('red', 'blue'); asr = 'UNMEASURED'; controls = $records}
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $OutputDirectory 'manifest.json'), (($manifest | ConvertTo-Json -Depth 6) + [Environment]::NewLine), $encoding)
} finally {
    $synthesizer.Dispose()
}
