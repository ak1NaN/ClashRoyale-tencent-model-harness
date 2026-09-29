import json

from ai_client.native_result import terminal_result, save_terminal_evidence
from ai_client.matches import MatchLedger


def write_end(directory, *, owner=0, marker=1, hp=None):
    directory.mkdir()
    hp = hp or {5000000: 100, 5000001: 100, 5000002: 100,
                5000003: 100, 5000004: 100, 5000005: 100}
    objects = [dict(id=identity, owner=(identity-5000000)//3, hp=value)
               for identity, value in hp.items()]
    frame = dict(battle_finished=True, native_end_marker=marker, owner=owner,
                 tick=3600, world='battle', generation=1, battlefield=dict(objects=objects))
    (directory/'events.jsonl').write_text(json.dumps(dict(at_ms=1,event='capture',value=frame))+'\n')


def test_native_terminal_result_confirms_destroyed_tower_and_king(tmp_path):
    win=tmp_path/'win'
    write_end(win,hp={5000000:100,5000001:100,5000002:100,
                      5000003:100,5000005:100})
    proof=terminal_result(win)
    assert proof['result']=='win' and proof['crowns']==dict(own=1,opponent=0)
    evidence=save_terminal_evidence(win,proof)
    ledger=MatchLedger(tmp_path/'matches')
    summary=dict(directory=str(win.resolve()),applied=1,error=None)
    ledger.record(summary,'win',str(evidence),source='native_terminal')
    assert ledger.rows()[0]['result_source']=='native_terminal'

    loss=tmp_path/'loss'
    write_end(loss,hp={5000000:0,5000001:100,5000002:100,
                       5000003:100,5000004:100,5000005:100})
    assert terminal_result(loss)['crowns']==dict(own=0,opponent=3)
    assert terminal_result(loss)['result']=='loss'


def test_native_terminal_does_not_guess_ties_or_invalid_markers(tmp_path):
    tied=tmp_path/'tied';write_end(tied)
    assert terminal_result(tied) is None
    invalid=tmp_path/'invalid';write_end(invalid,marker=0,
        hp={5000000:100,5000001:100,5000002:100,5000003:100,5000005:100})
    assert terminal_result(invalid) is None
    missing_king=tmp_path/'missing';write_end(missing_king,
        hp={5000000:100,5000001:100,5000002:100,5000005:100})
    assert terminal_result(missing_king) is None
