import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/runtime.json'

def defaults():
    return {
        'adb': str(ROOT / '.deps/platform-tools/adb'),
        'serial': '127.0.0.1:5555',
        'package': 'com.tencent.tmgp.supercell.clashroyale',
        'frida_host': '127.0.0.1:27042',
        'frida_server': str(ROOT / '.deps/frida/frida-server-17.17.0-android-arm64'),
        'expected_version': '38.4.2',
        'checkpoint': str(ROOT / 'models/hog26_2.pt'),
        'model_directory': str(ROOT / 'models'),
        'match_directory': str(ROOT / 'records/matches'),
        'continuous_count': 3,
        'min_card_gap_ms': 1000,
        'optimistic_entities': True,
        'continuous_targets': {},
        'card_names_config': str(ROOT / 'config/card_names_zh.json'),
        'firstlight': str(ROOT / 'third_party/FirstLight_CR'),
        'frame_directory': str(ROOT / 'records/live_frames'),
        'screen_width': 1440, 'screen_height': 2560,
        'arena': [0.08, 0.15, 0.92, 0.80],
        'hand': [[0.36, 0.89], [0.53, 0.89], [0.70, 0.89], [0.87, 0.89]],
        'calibrated': False, 'flip': False, 'max_age_ms': 750,
        'extended_public_mapping': True, 'touch_ms': 80, 'execution_backend': 'native',
    }

def load():
    result = defaults()
    if CONFIG.exists():
        result.update(json.loads(CONFIG.read_text()))
    result.pop('match_label', None)  # Retire the former editable filename tag.
    return result

def save(value):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
