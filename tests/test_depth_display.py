"""Metric depth colors agree across viewers without changing measurements."""

import warnings

import cv2
import numpy as np
import pytest

from femto_bolt_viewer.depth_display import colorize_depth_mm


def test_fixed_range_assigns_expected_turbo_colors_to_metric_anchors():
    values = np.array([[150, 475, 800]], np.uint16)
    expected = cv2.applyColorMap(np.array([[0, 127, 255]], np.uint8), cv2.COLORMAP_TURBO)

    actual = colorize_depth_mm(values)

    np.testing.assert_array_equal(actual, expected)
    assert actual.dtype == np.uint8
    assert actual.shape == (1, 3, 3)


def test_distant_background_cannot_change_foreground_distance_colors():
    before = np.array([[150, 300, 475, 700], [0, 400, 600, 800]], np.uint16)
    after = before.copy()
    after[1, 3] = 6000

    first, second = colorize_depth_mm(before), colorize_depth_mm(after)

    np.testing.assert_array_equal(first[0], second[0])
    np.testing.assert_array_equal(first[1, 3], second[1, 3])


def test_outside_display_interval_saturates_without_hiding_positive_measurements():
    values = np.array([[1, 149, 150, 800, 801, 65535]], np.uint16)
    colors = colorize_depth_mm(values)

    np.testing.assert_array_equal(colors[0, :3], np.repeat(colors[0, 2:3], 3, axis=0))
    np.testing.assert_array_equal(colors[0, 3:], np.repeat(colors[0, 3:4], 3, axis=0))
    assert np.all(np.any(colors != 0, axis=2))
    np.testing.assert_array_equal(values, [[1, 149, 150, 800, 801, 65535]])


def test_invalid_samples_are_black_without_warnings_or_input_mutation():
    values = np.array([[0, -1, np.nan, np.inf, -np.inf, 475]], np.float64)
    original = values.copy()
    values.flags.writeable = False

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with np.errstate(all="raise"):
            colors = colorize_depth_mm(values)

    np.testing.assert_array_equal(colors[0, :5], np.zeros((5, 3), np.uint8))
    assert np.any(colors[0, 5] != 0)
    np.testing.assert_array_equal(values, original)
    assert not values.flags.writeable


@pytest.mark.parametrize("upper", [2000, 6000])
def test_wider_display_ranges_preserve_endpoints_and_reveal_far_depths(upper):
    values = np.array([[150, (150 + upper) / 2, upper]], np.float64)
    expected = cv2.applyColorMap(np.array([[0, 127, 255]], np.uint8), cv2.COLORMAP_TURBO)

    colors = colorize_depth_mm(values, maximum_mm=upper)

    np.testing.assert_array_equal(colors, expected)
    assert not np.array_equal(colors[0, 1], colorize_depth_mm(values)[0, 1])


@pytest.mark.parametrize("bounds", [
    (-1, 800), (800, 800), (900, 800), (0, 0),
    (np.nan, 800), (150, np.nan), (-np.inf, 800), (150, np.inf),
])
def test_invalid_metric_display_bounds_are_refused(bounds):
    with pytest.raises(ValueError, match="bounds"):
        colorize_depth_mm(np.array([[400]], np.uint16), *bounds)


@pytest.mark.parametrize("values", [np.array(1), np.array([1]), np.zeros((1, 2, 3)), np.zeros((0, 2))])
def test_only_nonempty_two_dimensional_depth_rasters_are_renderable(values):
    with pytest.raises(ValueError, match="raster"):
        colorize_depth_mm(values)
