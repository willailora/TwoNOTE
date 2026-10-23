# Third-party software

TwoNote uses unmodified open-source libraries. Dependency license texts are
embedded in the Windows package under `licenses/`; the PyInstaller one-file
loader extracts that directory with the application's DLLs while running.

Qt / PySide6 / Shiboken 6.10.2 are used under LGPL-3.0. Qt modules not needed for
the Widgets application (PDF, VirtualKeyboard, QML/Quick, SVG) are omitted from
the Windows build. Source for the unmodified libraries is available from
https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.10.2 and
https://code.qt.io/cgit/qt/qtbase.git/?h=v6.10.2 (image plugins:
https://code.qt.io/cgit/qt/qtimageformats.git/?h=v6.10.2).
The LGPL and GPL texts are bundled. TwoNote's source and Windows build script
are provided in this project so the application can be rebuilt with compatible
modified Qt/PySide6 libraries. No restriction on debugging such modifications
is imposed by this project.

Other Python runtime/build components include cryptography, cffi, Pillow,
qrcode, aspose-note (the MIT-licensed Aspose.Note-FOSS-for-Python parser), and
PyInstaller (GPL with its bootloader exception). Their supplied copyright and
license notices are retained in the packaged `licenses/` directory. Optional
PDF-export dependencies of the OneNote parser are not used by TwoNote.

Android uses AndroidX AppCompat / Activity / Biometric, Kotlin standard library,
and ZXing / ZXing Android Embedded, under their Apache-2.0 licenses. Upstream:
https://android.googlesource.com/platform/frameworks/support/
https://github.com/JetBrains/kotlin
https://github.com/zxing/zxing
https://github.com/journeyapps/zxing-android-embedded

Tests use JUnit and Robolectric; they are not included in the release APK.
Robolectric configuration follows https://robolectric.org/getting-started/.
