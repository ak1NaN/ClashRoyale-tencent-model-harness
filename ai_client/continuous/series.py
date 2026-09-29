"""One confirmed result per round, with bounded waits and no blind re-clicks."""

MAX_MATCHES = 325
RESULT_SETTLE_MS = 3000


class ContinuousSeries:
    def __init__(self, *, arm, tap, stop_match, schedule, status):
        self.arm, self.tap, self.stop_match = arm, tap, stop_match
        self.schedule, self.status = schedule, status
        self.active = False
        self.phase = 'idle'
        self.completed = 0
        self.total = 0
        self.targets = {}
        self.directory = None
        self.generation = 0
        self.round_generation = 0

    def _later(self, delay_ms, callback):
        generation, round_generation = self.generation, self.round_generation
        self.schedule(delay_ms, lambda: callback() if self.active and
            self.generation == generation and self.round_generation == round_generation else None)

    def begin(self, total, targets):
        if self.active:
            raise RuntimeError('连战已经运行')
        if type(total) is not int or not 1 <= total <= MAX_MATCHES:
            raise ValueError(f'对战次数必须是 1～{MAX_MATCHES} 的整数')
        if any(kind not in targets for kind in ('start', 'end')):
            raise ValueError('请先拖动并保存开始、结束两个按钮的位置')
        self.total, self.completed = total, 0
        self.targets = {kind: dict(targets[kind]) for kind in ('start', 'end')}
        self.directory = None
        self.active = True
        self.phase = 'preparing'
        self.generation += 1
        self.status(f'连战准备中 · 计划 {total} 场')
        self._later(200, self._launch)

    def _launch(self):
        if not self.active:
            return
        # Invalidate every timeout scheduled for the previous match before
        # this one can enter the same phase (matchmaking, battle or result).
        self.round_generation += 1
        try:
            self.arm()
            self.directory = None
            self.phase = 'tapping_start'
            self.status(f'第 {self.completed+1}/{self.total} 场 · 已待命，点击开始对局')
            self.tap('start', self.targets['start'])
        except Exception as error:
            self.fail(str(error))

    def on_tap_result(self, kind, error=None):
        expected = {'start': 'tapping_start', 'end': 'tapping_end'}
        if not self.active or self.phase != expected.get(kind):
            return
        if error:
            self.fail(f'{kind} 点击失败：{error}')
            return
        if kind == 'start':
            self.phase = 'matchmaking'
            self.status(f'第 {self.completed+1}/{self.total} 场 · 等待进入对局')
            self._later(90_000, lambda: self._timeout('matchmaking', '90 秒内未进入对局'))
        elif self.completed == self.total:
            self.active = False
            self.phase = 'complete'
            self.generation += 1
            self.status(f'连战完成 · {self.completed}/{self.total} 场')
        else:
            self.phase = 'returning'
            self.status(f'已完成 {self.completed}/{self.total} 场 · 等待返回主界面')
            self._later(2500, self._launch)

    def on_battle_started(self, directory):
        if not self.active or self.phase not in ('tapping_start', 'matchmaking'):
            return
        self.directory = str(directory)
        self.phase = 'battle'
        self.status(f'第 {self.completed+1}/{self.total} 场 · 模型对战中')
        self._later(8 * 60_000, lambda: self._timeout('battle', '对局超过八分钟仍未结束'))

    def on_battle_finished(self, directory):
        if not self.active or self.phase not in ('battle', 'matchmaking', 'tapping_start'):
            return
        if self.directory is not None and str(directory) != self.directory:
            self.fail('终局与当前对局身份不一致')
            return
        self.directory = str(directory)
        self.phase = 'result'
        self.status(f'第 {self.completed+1}/{self.total} 场 · 等待结果确认')
        self._later(35_000, lambda: self._timeout('result', '结果未在 35 秒内确认'))

    def on_match_result(self, directory, result):
        if not self.active or self.phase != 'result':
            return
        if str(directory) != self.directory or result not in ('win', 'loss', 'draw'):
            self.fail('结果与当前对局不匹配')
            return
        self.completed += 1
        self.phase = 'settling'
        self.status(f'第 {self.completed}/{self.total} 场已确认 {result} · 等待结算稳定')
        self._later(RESULT_SETTLE_MS, self._tap_end)

    def _tap_end(self):
        if not self.active or self.phase != 'settling':
            return
        self.phase = 'tapping_end'
        self.status(f'第 {self.completed}/{self.total} 场 · 点击结束对局')
        try:
            self.tap('end', self.targets['end'])
        except Exception as error:
            self.fail(str(error))

    def _timeout(self, phase, message):
        if self.phase == phase:
            self.fail(message)

    def fail(self, message):
        if not self.active:
            return
        self.active = False
        self.phase = 'failed'
        self.generation += 1
        self.stop_match()
        self.status(f'连战停止 · 已完成 {self.completed}/{self.total} 场 · {message}')

    def stop(self):
        if self.active:
            self.active = False
            self.phase = 'stopped'
            self.generation += 1
            self.status(f'连战已手动停止 · 已完成 {self.completed}/{self.total} 场')
