"""Read-only Android manifest/resource checks, without a GUI or Android emulator."""
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1] / "android" / "app" / "src" / "main"
A = "{http://schemas.android.com/apk/res/android}"


class PhotoConfigurationTests(unittest.TestCase):
    def test_provider_is_private_and_grants_only_specific_uris(self):
        manifest = ET.parse(ROOT / "AndroidManifest.xml").getroot()
        provider = manifest.find("application/provider")
        self.assertEqual(provider.get(A + "name"), ".CaptureProvider")
        self.assertEqual(provider.get(A + "exported"), "false")
        self.assertEqual(provider.get(A + "grantUriPermissions"), "true")
        self.assertEqual(provider.get(A + "authorities"), "${applicationId}.capture")
        self.assertEqual(provider.find("meta-data").get(A + "resource"), "@xml/capture_paths")

    def test_only_dedicated_internal_camera_cache_is_exposed(self):
        paths = ET.parse(ROOT / "res" / "xml" / "capture_paths.xml").getroot()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].tag, "cache-path")
        self.assertEqual(paths[0].attrib, {"name": "camera", "path": "twonote-camera/"})

    def test_no_photo_library_or_external_storage_permission(self):
        manifest = ET.parse(ROOT / "AndroidManifest.xml").getroot()
        permissions = {p.get(A + "name") for p in manifest.findall("uses-permission")}
        self.assertIn("android.permission.CAMERA", permissions)
        for name in ("READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "MANAGE_EXTERNAL_STORAGE"):
            self.assertNotIn("android.permission." + name, permissions)
        features = {p.get(A + "name"): p.get(A + "required") for p in manifest.findall("uses-feature")}
        self.assertEqual(features["android.hardware.camera"], "false")


if __name__ == "__main__":
    unittest.main()
