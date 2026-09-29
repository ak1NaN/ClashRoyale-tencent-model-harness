"""Append-only match outcomes with evidence and no selective streak counting."""
import json
from pathlib import Path
from .config import ROOT

class MatchLedger:
    def __init__(self, match_directory=None):
        self.path=Path(match_directory).expanduser()/'results.jsonl' if match_directory else ROOT/'records/matches/results.jsonl'
        self.path.parent.mkdir(parents=True,exist_ok=True)
    def rows(self):
        if not self.path.exists():return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line]
    def record(self,summary,result,evidence,source='native_terminal'):
        if result not in ('win','loss','draw','aborted'):raise ValueError('无效结果')
        if not summary or not summary.get('directory'):raise ValueError('没有可关联的比赛')
        if any(r['directory']==summary['directory'] for r in self.rows()):raise ValueError('本场结果已保存')
        if result=='win' and (summary.get('error') and not summary.get('verified_terminal_timeout') or summary.get('applied',0)==0):
            raise ValueError('异常或无实际模型下牌的比赛不能计入胜场')
        if result!='aborted' and (not evidence or not Path(evidence).is_file()):raise ValueError('需要已保存的结果证据')
        if source!='native_terminal':raise ValueError('仅支持本地原生终局证据')
        proof=json.loads(Path(evidence).read_text()) if evidence and Path(evidence).is_file() else {}
        if result!='aborted' and (proof.get('source')!='native_terminal' or proof.get('directory')!=summary['directory']
                or proof.get('result')!=result or proof.get('native_end_marker',0)<=0):
            raise ValueError('原生终局证据与本局不匹配')
        row=dict(summary,result=result,evidence=evidence,result_source=source,mode='1v1')
        with self.path.open('a') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
        return self.stats()
    def stats(self):
        recorded={r['directory']:r for r in self.rows()}
        # Finished but unconfirmed attempts interrupt a streak as well. Restarting
        # the GUI or omitting a bad result cannot remove an attempted match.
        for path in self.path.parent.glob('*/summary.json'):
            summary=json.loads(path.read_text())
            recorded.setdefault(summary['directory'],dict(summary,result='unconfirmed'))
        rows=sorted(recorded.values(),key=lambda r:r['directory']);streak=0
        for row in rows:streak=streak+1 if row['result']=='win' else 0
        return dict(matches=len(rows),wins=sum(r['result']=='win' for r in rows),streak=streak,target=3)
