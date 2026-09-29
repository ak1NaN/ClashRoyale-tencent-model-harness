import json
from datetime import datetime
from pathlib import Path

from ai_client.config import ROOT
from ai_client.match_files import link_labeled_events


def test_labeled_jsonl_keeps_canonical_capture_and_identifies_match(tmp_path):
    directory = tmp_path / '20260928-120000-abc123'
    directory.mkdir()
    source = directory / 'events.jsonl'
    source.write_text('{"event":"armed"}\n')
    named = link_labeled_events(directory, started_at=datetime(2026, 9, 28, 12, 0),
                                label='我的 1v1 测试', checkpoint='/models/hog26_2.pt')
    assert named.name == '2026-09-28_12-00-00_我的-1v1-测试_hog26_2_abc123.jsonl'
    assert named.samefile(source)
    assert link_labeled_events(directory, started_at=datetime(2026, 9, 28, 12, 0),
                               label='我的 1v1 测试', checkpoint='/models/hog26_2.pt') == named


def test_cn_card_config_covers_existing_card_catalog():
    catalog = json.loads((ROOT/'config/card_catalog.json').read_text())['items']
    names = json.loads((ROOT/'config/card_names_zh.json').read_text())['cards']
    by_id = {row['id']: row for row in names}
    assert all(row['id'] in by_id and by_id[row['id']]['zh'] for row in catalog)
    assert by_id[26000021]['zh'] == '野猪骑士'
