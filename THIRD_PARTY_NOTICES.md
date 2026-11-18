# Third-party notices

The original application code is GPL-3.0-or-later; see [LICENSE](LICENSE) and
[NOTICE](NOTICE). This file identifies dependencies of the native USB capture
build. It does not change their licenses or grant rights to proprietary resources.

## Pinned public SDK source

The build uses [OrbbecSDK_v2](https://github.com/orbbec/OrbbecSDK_v2) at commit
`575347ca742d219ef61b1e214e97043f17f48d23`. Its public source is MIT-licensed.
The source checkout is obtained separately during the build, not vendored here.
The build prepares a modified translation unit to enable Femto Bolt enumeration
on macOS; the upstream checkout is retained unchanged. The generated source must
retain its original copyright and license notices.

The actual capture link chain contains the SDK platform, core, and shared source
libraries and the components below. Other libraries present in the full SDK
checkout are not necessarily linked into this project's capture helper.

| Component | License | Complete included notice | Original path in pinned SDK |
| --- | --- | --- | --- |
| Orbbec public SDK source | MIT | [OrbbecSDK-LICENSE.txt](docs/licenses/OrbbecSDK-LICENSE.txt) | `LICENSE.txt` (repository source section) |
| libusb | LGPL-2.1-or-later | [libusb-COPYING.txt](docs/licenses/libusb-COPYING.txt) | `3rdparty/libusb/src/COPYING` |
| libuvc | BSD-3-Clause | [libuvc-LICENSE.txt](docs/licenses/libuvc-LICENSE.txt) | `3rdparty/libuvc/src/LICENSE.txt` |
| spdlog | MIT | [spdlog-LICENSE.txt](docs/licenses/spdlog-LICENSE.txt) | `3rdparty/spdlog/src/LICENSE` |
| bundled fmt | MIT with optional exception | [fmt-LICENSE.txt](docs/licenses/fmt-LICENSE.txt) | `3rdparty/spdlog/src/include/spdlog/fmt/bundled/fmt.license.rst` |
| tinyxml2 | zlib | [tinyxml2-LICENSE.txt](docs/licenses/tinyxml2-LICENSE.txt) | `3rdparty/tinyxml2/LICENSE.txt` |
| cmrc | MIT | [cmrc-LICENSE.txt](docs/licenses/cmrc-LICENSE.txt) | `3rdparty/cmrc/LICENSE.txt` |

The capture helper links libusb statically. A distributor of compiled helpers
must satisfy the applicable source, notice, and rebuilding/relinking requirements;
providing only an executable and this table is not enough. Keep the corresponding
source revision, local modifications, and build instructions available together
with any binary distribution. Python dependencies are installed separately by
pip and retain their own distribution metadata and license notices.

## Proprietary SDK extensions are separate

The upstream `LICENSE.txt` includes terms for proprietary SDK extension binaries
as well as the public source license. The complete upstream text is preserved
above to avoid confusing those scopes. The upstream
[End User License Agreement](docs/licenses/OrbbecSDK-EULA.txt) is also preserved
for reference. Its open-source clause leaves the applicable open-source licenses
in control of the public components; extension use and distribution have separate
conditions and restrictions.

This repository does not include proprietary extension binaries, translated vendor
compute shaders, native profile resources, or device factory calibration. A user's
ability to run a local resource does not establish permission to publish it.
Contributions and releases must identify the provenance and applicable permissions
of any additional resource before including it.
