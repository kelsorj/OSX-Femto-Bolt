"""Browser API boundaries and lifecycle, with no camera or native process."""
from __future__ import annotations

from unittest.mock import Mock
import base64

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from femto_bolt_viewer.server import create_app
from femto_bolt_viewer.runtime import DemoViewer


@pytest.fixture
def viewer():
    item = Mock()
    item.snapshot.return_value = {"connected": False, "error": "Waiting for camera", "demo": False}
    return item


def test_status_is_no_store_without_image_encoding_and_lifespan_closes(viewer):
    with TestClient(create_app(viewer)) as client:
        viewer.start.assert_called_once_with()
        response = client.get("/status")
        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        viewer.snapshot.assert_called_once_with(include_images=False)

    viewer.close.assert_called_once_with()


def test_start_failure_still_releases_viewer(viewer):
    viewer.start.side_effect = RuntimeError("startup rejected")

    with pytest.raises(RuntimeError, match="startup rejected"):
        with TestClient(create_app(viewer)):
            pytest.fail("startup should not have completed")

    viewer.close.assert_called_once_with()


def test_foreign_host_cannot_access_local_status(viewer):
    with TestClient(create_app(viewer)) as client:
        response = client.get("/status", headers={"host": "example.invalid"})

    assert response.status_code == 400
    viewer.snapshot.assert_not_called()


@pytest.mark.parametrize("maximum", [800, 2000, 6000])
def test_websocket_uses_requested_fixed_display_range(viewer, maximum):
    with TestClient(create_app(viewer)) as client:
        with client.websocket_connect(f"/frames?max_mm={maximum}", headers={"origin": "http://testserver"}) as ws:
            message = ws.receive_json()

    assert message == viewer.snapshot.return_value
    assert viewer.snapshot.call_args.kwargs == {"max_mm": maximum}


@pytest.mark.parametrize("maximum", [-1, 0, 799, 65535])
def test_unsupported_display_ranges_are_rejected_before_sending_frames(viewer, maximum):
    with TestClient(create_app(viewer)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"/frames?max_mm={maximum}"):
                pytest.fail("unsupported range was accepted")

    assert exc.value.code == 1008
    viewer.snapshot.assert_not_called()


@pytest.mark.parametrize("origin", ["http://evil.invalid", "https://testserver", "null", "http://testserver:80"])
def test_cross_origin_browser_cannot_read_camera_frames(viewer, origin):
    with TestClient(create_app(viewer)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/frames", headers={"origin": origin}):
                pytest.fail("cross-origin socket was accepted")

    assert exc.value.code == 1008
    viewer.snapshot.assert_not_called()


def test_demo_websocket_transports_both_images_with_unambiguous_synthetic_status():
    with TestClient(create_app(DemoViewer())) as client:
        with client.websocket_connect("/frames") as ws:
            message = ws.receive_json()

    color = cv2.imdecode(np.frombuffer(base64.b64decode(message["color_jpeg"]), np.uint8), cv2.IMREAD_COLOR)
    depth = cv2.imdecode(np.frombuffer(base64.b64decode(message["depth_jpeg"]), np.uint8), cv2.IMREAD_COLOR)
    assert message["demo"] is True
    assert message["live"] is False
    assert message["connected"] is False
    assert color.shape == (720, 1280, 3)
    assert depth.shape == (512, 512, 3)
