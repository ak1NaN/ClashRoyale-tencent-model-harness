import json
import pytest
import ai_client.matches as matches

def test_unconfirmed_finished_attempt_breaks_streak(tmp_path,monkeypatch):
    monkeypatch.setattr(matches,'ROOT',tmp_path);ledger=matches.MatchLedger()
    for number in ('1','2'):
        directory=ledger.path.parent/number;directory.mkdir()
        summary=dict(directory=str(directory),error=None,applied=3)
        (directory/'summary.json').write_text(json.dumps(summary))
        if number=='1':
            evidence=directory/'terminal-evidence.json'
            evidence.write_text(json.dumps(dict(source='native_terminal',directory=str(directory),
                                               result='win',native_end_marker=1)))
            ledger.record(summary,'win',str(evidence))
    assert ledger.stats()==dict(matches=2,wins=1,streak=0,target=3)

def test_missing_evidence_and_failed_model_do_not_count_as_win(tmp_path,monkeypatch):
    monkeypatch.setattr(matches,'ROOT',tmp_path);ledger=matches.MatchLedger()
    summary=dict(directory=str(tmp_path/'a'),error=None,applied=3)
    with pytest.raises(ValueError,match='证据'):ledger.record(summary,'win',str(tmp_path/'missing.png'))
    with pytest.raises(ValueError,match='异常'):ledger.record(dict(summary,error='stale'),'win','x')

def test_ocr_result_is_not_accepted_in_source_release(tmp_path):
    ledger=matches.MatchLedger(tmp_path)
    directory=tmp_path/'round';directory.mkdir()
    evidence=directory/'result.png';evidence.write_bytes(b'image')
    summary=dict(directory=str(directory),error=None,applied=3)
    with pytest.raises(ValueError,match='仅支持本地原生终局证据'):
        ledger.record(summary,'win',str(evidence),source='ocr')
