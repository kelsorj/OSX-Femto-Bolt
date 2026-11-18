"""Explicit supported live modes; transport sizes are not depth-image sizes.

Transport and engine mapping follow SDK v2.10.6 RawPhaseStreamer.cpp. Optical
specifications follow https://www.orbbec.com/products/tof-camera/femto-bolt/ .
The advertised minimum distance describes the operating range, not a correction
or threshold applied to measured depth.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DepthMode:
    name: str
    engine_mode: int
    width: int
    height: int
    transport_width: int
    transport_height: int
    label: str
    hfov_deg: int
    vfov_deg: int
    min_distance_mm: int
    captures: int = 9

    @property
    def raw_bytes(self) -> int:
        return self.width * self.height * self.captures * 2

    @property
    def transport_bytes(self) -> int:
        return self.transport_width * self.transport_height * 2

    @property
    def output_bytes(self) -> int:
        return self.width * self.height * 4  # uint16 Z followed by uint16 amplitude


_MODES = {
    "nfov": DepthMode("nfov", 4, 640, 576, 7680, 434, "Narrow field", 75, 65, 500),
    "wfov-binned": DepthMode("wfov-binned", 7, 512, 512, 8192, 290, "Wide field (2×2 binned)", 120, 120, 250),
}
SUPPORTED_MODES = tuple(_MODES)


def get_mode(name: str) -> DepthMode:
    if not isinstance(name, str) or name not in _MODES:
        raise ValueError(f"Depth mode must be one of {', '.join(SUPPORTED_MODES)}")
    return _MODES[name]


get_depth_mode = get_mode
