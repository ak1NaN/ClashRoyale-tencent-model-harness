"""Map a draggable desktop marker into MuMu's Android display."""

import json
from pathlib import Path
import subprocess
import sys

from ..config import ROOT

SOURCE = Path(__file__).with_name('window_bounds.swift')
BINARY = ROOT / 'build/continuous_window_bounds'


def emulator_window():
    if sys.platform != 'darwin':
        raise RuntimeError('屏幕浮标定位目前支持 macOS MuMu 模拟器')
    if not BINARY.exists() or BINARY.stat().st_mtime < SOURCE.stat().st_mtime:
        BINARY.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['swiftc', str(SOURCE), '-o', str(BINARY)], check=True,
                       capture_output=True, timeout=90)
    try:
        output = subprocess.check_output([str(BINARY)], timeout=5, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('未找到正在显示的 MuMu 安卓设备窗口') from error
    return json.loads(output)


def marker_to_android(point, window, screen_size):
    """MuMu fits the Android image to window width below its top toolbar."""
    x, y = map(float, point)
    width, height = map(float, screen_size)
    wx, wy = float(window['x']), float(window['y'])
    ww, wh = float(window['width']), float(window['height'])
    if min(width, height, ww, wh) <= 0:
        raise ValueError('无效的模拟器窗口或屏幕尺寸')
    image_height = ww * height / width
    if image_height > wh or not 0 <= wh - image_height <= 120:
        raise ValueError('MuMu 窗口比例异常；请保持游戏画面完整可见后重新定位')
    left, top = wx, wy + wh - image_height
    u, v = (x - left) / ww, (y - top) / image_height
    if not 0 <= u <= 1 or not 0 <= v <= 1:
        raise ValueError('请把浮标中心拖到 MuMu 游戏画面内部')
    return {'u': u, 'v': v, 'desktop_x': round(x), 'desktop_y': round(y)}


def target_pixels(target, screen_size):
    width, height = map(int, screen_size)
    if width <= 0 or height <= 0:
        raise ValueError('无效的 Android 屏幕尺寸')
    u, v = float(target['u']), float(target['v'])
    if not 0 <= u <= 1 or not 0 <= v <= 1:
        raise ValueError('点击点超出 Android 屏幕')
    return round(u * (width - 1)), round(v * (height - 1))
