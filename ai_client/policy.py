"""Serial requests to the unchanged upstream policy service."""
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from .config import ROOT

class PolicyProcess:
    def __init__(self, config):
        self.config = config
        self.process = None
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.log = None

    def start(self):
        checkpoint = Path(self.config['checkpoint'])
        if not checkpoint.is_file() or checkpoint.stat().st_size < 1024:
            raise RuntimeError('模型文件不存在或内容为空')
        env = dict(os.environ, PYTHONPATH=self.config['firstlight'])
        log_path = ROOT / 'records/model-stderr.log'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open('a')
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'tools/policy_worker.py'),
                                         '--checkpoint', str(checkpoint)],
            cwd=self.config['firstlight'], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        def read():
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    self.responses.put({'ok': False, 'error': '模型协议返回非 JSON'})
            self.responses.put({'ok': False, 'error': '模型进程已退出，请查看 records/model-stderr.log'})
        threading.Thread(target=read, daemon=True).start()
        response = self.responses.get(timeout=120)
        if not response.get('ok'):
            raise RuntimeError(response.get('error'))
        return response

    def request(self, payload):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError('模型未运行')
            self.process.stdin.write(json.dumps(payload) + '\n')
            self.process.stdin.flush()
            try:
                result = self.responses.get(timeout=8)
            except queue.Empty:
                self.close()
                raise RuntimeError('模型推理超时，已停止模型')
            if not result.get('ok'):
                raise RuntimeError(result.get('error', '模型请求失败'))
            return result

    def close(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None
        if self.log:
            self.log.close()
            self.log = None
