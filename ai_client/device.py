import io
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from .config import ROOT
from .bridge import NativeQueueStateChanged, NativeStream, deployment_request

class AndroidDevice:
    def __init__(self, config):
        self.config = config
        self.session = self.script = None
        self.detached = threading.Event()
        self.connected = False
        self.stream = NativeStream()

    def synchronize_clock(self):
        if self.script is None:
            raise RuntimeError('原生探针尚未连接')
        samples = []
        for _ in range(5):
            before = time.time_ns() / 1_000_000
            guest = self.script.exports_sync.nativeclock()
            after = time.time_ns() / 1_000_000
            samples.append((after-before, (before+after)/2-guest))
        self.stream.clock_offset_ms = min(samples)[1]
        return self.stream.clock_offset_ms

    def adb(self, *args, binary=False, timeout=12):
        result = subprocess.run([self.config['adb'], '-s', self.config['serial'], *map(str, args)],
                                capture_output=True, timeout=timeout, check=True)
        return result.stdout if binary else result.stdout.decode().strip()

    def connect(self, callback):
        import frida
        subprocess.run([self.config['adb'], 'connect', self.config['serial']],
                       capture_output=True, timeout=12, check=True)
        if self.adb('get-state') != 'device':
            raise RuntimeError('模拟器不在线')
        package = self.config['package']
        version_text = self.adb('shell', 'dumpsys', 'package', package)
        version = re.search(r'versionName=(\S+)', version_text)
        if not version or version.group(1) != self.config['expected_version']:
            raise RuntimeError('客户端版本与探针配置不符，需要迁移偏移')
        if 'uid=0' not in self.adb('shell', 'id'):
            raise RuntimeError('ADB 未取得 Root')
        try:
            server_pid = self.adb('shell', 'pidof', 'frida-server')
        except subprocess.CalledProcessError:
            server_pid = ''
        if not server_pid:
            local = Path(self.config['frida_server'])
            if not local.is_file():
                raise RuntimeError('缺少 Frida Server 文件')
            self.adb('push', local, '/data/local/tmp/frida-server')
            self.adb('shell', 'chmod', '755', '/data/local/tmp/frida-server')
            self.adb('shell', 'nohup /data/local/tmp/frida-server >/data/local/tmp/tencent-ai-frida.log 2>&1 &')
        port = int(self.config['frida_host'].rsplit(':', 1)[1])
        self.adb('forward', f'tcp:{port}', 'tcp:27042')
        pid = self.adb('shell', 'pidof', package)
        if not pid:
            raise RuntimeError('请先打开腾讯客户端')
        manager = frida.get_device_manager().add_remote_device(self.config['frida_host'])
        self.session = manager.attach(int(pid.split()[0]))
        self.session.on('detached', lambda *args: self.detached.set())
        self.stream = NativeStream()
        profiles = json.loads((ROOT / 'config/native_offsets.json').read_text())
        offsets = profiles.get(self.config['expected_version'])
        if offsets is None:
            raise RuntimeError('没有此客户端版本的原生偏移配置')
        preflight = ('globalThis.__tencentOffsets=' + json.dumps(offsets, separators=(',', ':')) + ';\n'
                     'globalThis.__tencentEntryWords={}; for (const off of '
                     '[__tencentOffsets.step,__tencentOffsets.clock,__tencentOffsets.stock_submit,'
                     '__tencentOffsets.command_ctor,__tencentOffsets.parse_json,__tencentOffsets.selection,'
                     '__tencentOffsets.alloc,__tencentOffsets.get_mana,__tencentOffsets.resolve_player,'
                     '__tencentOffsets.record_ctor]) '
                     "globalThis.__tencentEntryWords[off]=Process.getModuleByName('libg.so').base.add(off).readU32();\n")
        source = preflight + (ROOT / 'probes/tencent_native_runtime.js').read_text() + '\n' + (ROOT / 'probes/tencent_overlay.js').read_text()
        self.script = self.session.create_script(source)
        def on_message(message, data):
            if message.get('type') == 'send':
                self.stream.ingest(message['payload'])
                callback(message['payload'])
            elif message.get('type') == 'error':
                error = {'event': 'probe_error', 'message': message.get('stack', str(message))}
                self.stream.ingest(error)
                callback(error)
        self.script.on('message', on_message)
        self.script.load()
        if self.stream.failure:
            raise RuntimeError(self.stream.failure)
        # ADB/Frida guest time can differ from macOS by hundreds of ms.
        self.synchronize_clock()
        size = self.adb('shell', 'wm', 'size')
        width, height = re.findall(r'(\d+)x(\d+)', size)[-1]
        previous = (self.config['screen_width'], self.config['screen_height'])
        self.config.update(screen_width=int(width), screen_height=int(height))
        if previous != (int(width), int(height)):
            self.config['calibrated'] = False
        self.detached.clear()
        self.connected = True
        return int(pid.split()[0])

    def native_status(self):
        if not self.connected:
            raise RuntimeError('设备未连接')
        return self.script.exports_sync.nativestatus()

    def enable_native(self, enabled):
        if not self.connected:
            raise RuntimeError('设备未连接')
        return self.script.exports_sync.nativeenable(bool(enabled))

    def queue_action(self, action):
        snapshot = self.stream.latest()
        if not self.connected or snapshot is None:
            raise RuntimeError('没有有效原生观测')
        request = deployment_request(action, snapshot)
        try:
            queued = self.script.exports_sync.nativequeue(request)
        except Exception as exc:
            message = str(exc)
            # These errors happen before pending.push() in the guest. The
            # action was never submitted, so the live battle can continue.
            if request.get('kind') == 'activate_ability' and any(marker in message for marker in (
                'Ability state changed before queue:', 'Ability already queued:',
                'Ability is unavailable or already queued',  # Older injected script.
            )):
                raise NativeQueueStateChanged(message) from exc
            raise
        return {'request': request, 'queued': queued}

    def cancel_native(self):
        if self.script:
            return self.script.exports_sync.nativecancel()

    def screenshot(self):
        from PIL import Image
        return Image.open(io.BytesIO(self.adb('exec-out', 'screencap', '-p', binary=True))).convert('RGB')

    def foreground(self):
        text = self.adb('shell', 'dumpsys', 'window')
        return any(self.config['package'] in line for line in text.splitlines()
                   if 'mCurrentFocus' in line or 'mFocusedApp' in line)

    def drag(self, start, end):
        self.adb('shell', 'input', 'swipe', *start, *end, self.config['touch_ms'])

    def close(self):
        self.connected = False
        self.detached.set()
        session, script = self.session, self.script
        self.session = self.script = None
        # Some emulator Frida sessions block indefinitely while detaching.
        # Keep Stop and the Tk main loop responsive even in that case.
        def detach():
            try:
                if script:
                    try:
                        script.exports_sync.nativecancel()
                    except Exception:
                        pass
                    script.unload()
                if session:
                    session.detach()
            except Exception:
                pass
        if session:
            threading.Thread(target=detach, daemon=True).start()
