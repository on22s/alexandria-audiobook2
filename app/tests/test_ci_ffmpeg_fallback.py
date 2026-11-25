"""Windows CI installs ffmpeg through one pinned, checksum-verified script."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'tools/ci/install_ffmpeg_windows.ps1'
WORKFLOWS = ROOT / '.github/workflows'

ARCHIVE_URL = ('https://github.com/GyanD/codexffmpeg/releases/download/'
               '9.1/ffmpeg-9.1-essentials_build.zip')
ARCHIVE_SHA256 = 'b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd'
INVOCATION = ('pwsh -NoProfile -ExecutionPolicy Bypass -File '
              '"${{ github.workspace }}/tools/ci/install_ffmpeg_windows.ps1"')
INSTALL_WINDOWS_WORKFLOWS = (
    'tests.yml',
    'qwen-readalong.yml',
    'sound-assets.yml',
    'external-tts-lifetime.yml',
)
PATH_FILTERED_WORKFLOWS = (
    'qwen-readalong.yml',
    'sound-assets.yml',
    'external-tts-lifetime.yml',
)


class WindowsFfmpegInstallTests(unittest.TestCase):
    def test_no_workflow_installs_ffmpeg_with_chocolatey_directly(self):
        for path in sorted(WORKFLOWS.glob('*.yml')):
            with self.subTest(workflow=path.name):
                self.assertNotIn('choco install ffmpeg', path.read_text(encoding='utf-8'))

    def test_every_windows_install_step_uses_the_shared_script(self):
        self.assertTrue(INSTALLER.is_file(), INSTALLER)
        for name in INSTALL_WINDOWS_WORKFLOWS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding='utf-8')
                self.assertEqual(1, text.count(INVOCATION))

    def test_path_filters_trigger_on_the_shared_script(self):
        for name in PATH_FILTERED_WORKFLOWS:
            with self.subTest(workflow=name):
                text = (WORKFLOWS / name).read_text(encoding='utf-8')
                trigger = text.split('  pull_request:', 1)[1].split('  merge_group:', 1)[0]
                self.assertIn("      - 'tools/ci/install_ffmpeg_windows.ps1'", trigger)

    def test_installer_pins_url_and_sha256_and_verifies_before_extracting(self):
        source = INSTALLER.read_text(encoding='utf-8')
        self.assertIn(f"$ArchiveUrl = '{ARCHIVE_URL}'", source)
        self.assertIn(f"$ArchiveSha256 = '{ARCHIVE_SHA256}'", source)
        self.assertIn('Get-FileHash -Algorithm SHA256', source)
        self.assertLess(source.index('Get-FileHash -Algorithm SHA256'),
                        source.index('Expand-Archive'))
        self.assertIn('$env:GITHUB_PATH', source)


if __name__ == '__main__':
    unittest.main()
