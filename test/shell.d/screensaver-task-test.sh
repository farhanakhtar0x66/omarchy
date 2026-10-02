#!/bin/bash

set -euo pipefail

source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/base-test.sh"

require_command python3

python3 - "$ROOT" <<'PY'
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

root = Path(sys.argv[1])
wrapper = str(root / "bin/omarchy-screensaver-run")

with tempfile.TemporaryDirectory() as directory:
  home = Path(directory)
  tools = home / "bin"
  tools.mkdir()
  engine = tools / "ttfx"
  engine.write_text("#!/bin/bash\nsleep 0.1\n")
  engine.chmod(0o755)
  art = home / ".config/omarchy/branding/screensaver.txt"
  art.parent.mkdir(parents=True)
  art.write_text("TEST\n")
  env = dict(os.environ, HOME=directory, OMARCHY_PATH=str(root),
             XDG_STATE_HOME=str(home / "state"), PATH=f"{tools}:{root / 'bin'}:{os.environ['PATH']}")
  command = [sys.executable, "-c", "import sys; print('plain-output'); sys.exit(7)"]
  result = subprocess.run([wrapper, *command], env=env, capture_output=True, text=True)
  assert result.returncode == 7 and result.stdout == "plain-output\n", result
  assert not (home / "state").exists()
  print("ok - disabled task screensaver preserves normal output and exit status")

  flag = home / ".local/state/omarchy/toggles/screensaver-task-on"
  flag.parent.mkdir(parents=True)
  flag.touch()
  result = subprocess.run([wrapper, *command], env=env, capture_output=True, text=True)
  assert result.returncode == 7 and result.stdout == "plain-output\n", result
  assert not (home / "state").exists()
  print("ok - redirected commands bypass the animation even when enabled")

  def terminal_task(program, key=None, cancel=False):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    settings = termios.tcgetattr(slave)
    process = subprocess.Popen([wrapper, sys.executable, "-c", program], env=env,
                               stdin=slave, stdout=slave, stderr=slave)
    output = bytearray()
    started = time.monotonic()
    sent = False
    try:
      while time.monotonic() - started < 10:
        if not sent and time.monotonic() - started > 0.4:
          if key:
            os.write(master, key)
          if cancel:
            process.send_signal(signal.SIGINT)
          sent = True
        if select.select([master], [], [], 0.05)[0]:
          output.extend(os.read(master, 65536))
        elif process.poll() is not None:
          break
      status = process.wait(timeout=1)
      assert termios.tcgetattr(slave) == settings, "TTY settings were not restored"
      return status, bytes(output)
    finally:
      if process.poll() is None:
        process.kill()
        process.wait()
      os.close(master)
      os.close(slave)

  status, output = terminal_task("import sys,time; print('task-output'); print('task-error',file=sys.stderr); time.sleep(.2); sys.exit(7)")
  assert status == 7, (status, output)
  assert b"task-output" in output and b"task-error" in output
  assert b"\x1b[?1049l" in output
  logs = list((home / "state/omarchy/screensaver-tasks").glob("*.log"))
  assert len(logs) == 1
  assert "task-output" in logs[0].read_text() and "task-error" in logs[0].read_text()
  print("ok - completion restores the terminal, replays stdout/stderr, retains the log and task status")

  status, output = terminal_task("import time; time.sleep(.7); print('finished-after-dismissal')", key=b"q")
  assert status == 0 and b"finished-after-dismissal" in output, (status, output)
  print("ok - dismissing the animation does not cancel the task")

  worker_pid = home / "worker.pid"
  worker_program = f"import os,signal,time; from pathlib import Path; signal.signal(signal.SIGINT, signal.SIG_IGN); Path({str(worker_pid)!r}).write_text(str(os.getpid())); time.sleep(30)"
  task_program = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{worker_program!r}]); time.sleep(30)"
  status, output = terminal_task(task_program, cancel=True)
  assert status == 130, (status, output)
  assert worker_pid.exists()
  pid = int(worker_pid.read_text())
  # A killed orphan can remain a zombie until the test host's init reaps it.
  stat = Path(f"/proc/{pid}/stat")
  assert not stat.exists() or stat.read_text().split()[2] == "Z", "task worker survived cancellation"
  print("ok - Ctrl-C cancels the task and its workers and returns 130")
PY
