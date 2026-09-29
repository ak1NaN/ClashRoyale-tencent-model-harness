"""A readiness handshake around FirstLight's original JSON-lines service."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torch
from model_demo import run_demo, check_live_cards
from live_mapping import LivePolicy
from native_runner.training.v4.policy_session import load_policy_v4
from native_runner.training.v4.serve_policy import PolicyService

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    loaded = load_policy_v4(args.checkpoint, device='cpu')
    service = PolicyService(loaded, device='cpu', sample=False)
    live = LivePolicy(loaded)
    print(json.dumps({'ok': True, 'event': 'ready', 'checkpoint_id': loaded.checkpoint_id,
                      'sha256': loaded.checkpoint_sha256}), flush=True)
    try:
        for line in sys.stdin:
            request = {}
            try:
                request = json.loads(line)
                if request.get('op') in ('live_start', 'live_step'):
                    result = live.handle(request)
                elif request.get('op') == 'live_end':
                    live.end()
                    result = {'ok': True}
                elif request.get('op') == 'demo':
                    result = run_demo(service)
                elif request.get('op') == 'check_live_input':
                    result = check_live_cards(request['snapshot'])
                else:
                    result = service.handle(request)
                encoded = json.dumps(result)
            except Exception as error:
                if request.get('op', '').startswith('live_'): live.end()
                if request.get('op') != 'check_live_input':
                    service.end()
                result = {'ok': False, 'error': str(error)}
                encoded = json.dumps(result)
            print(encoded, flush=True)
    finally:
        live.end()
        service.end()

if __name__ == '__main__':
    main()
