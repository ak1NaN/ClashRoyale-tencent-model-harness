"""Single desktop controller; the lock automatically releases after a crash."""
import fcntl
from ai_client.config import ROOT
from ai_client.gui import main

if __name__ == '__main__':
    (ROOT/'records').mkdir(exist_ok=True)
    with (ROOT/'records/gui.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('FirstLight GUI 已运行，请使用现有窗口。')
        main()
