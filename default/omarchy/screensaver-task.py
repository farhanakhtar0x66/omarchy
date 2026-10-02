"""Animate the current terminal during a non-interactive command."""

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty


def cancel_task(task):
  if task.poll() is not None:
    return
  for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
    try:
      os.killpg(task.pid, sig)
    except ProcessLookupError:
      return
    try:
      task.wait(timeout=2)
      # The parent may exit before its workers. Finish cancelling the whole group.
      try:
        os.killpg(task.pid, signal.SIGTERM)
        time.sleep(0.1)
        os.killpg(task.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      return
    except subprocess.TimeoutExpired:
      pass


def run(command):
  art = Path.home() / ".config/omarchy/branding/screensaver.txt"
  if not art.is_file() or not art.read_text().strip():
    print("Screensaver artwork is missing or empty", file=sys.stderr)
    return 1

  state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
  logs = state / "omarchy/screensaver-tasks"
  logs.mkdir(parents=True, exist_ok=True)
  animation = None
  dismissed = False
  cancelled = 0
  resized = False

  with tempfile.NamedTemporaryFile(dir=logs, prefix="task-", suffix=".log", delete=False) as output:
    log_path = Path(output.name)
    try:
      task = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                              stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
      print(f"Could not start command: {error}", file=sys.stderr)
      return 127

    def stop(signum, frame):
      nonlocal cancelled
      cancelled = signum
      if animation is not None and animation.poll() is None:
        animation.terminate()

    def resize(signum, frame):
      nonlocal resized
      resized = True
      if animation is not None and animation.poll() is None:
        animation.terminate()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
      signal.signal(sig, stop)
    signal.signal(signal.SIGWINCH, resize)
    fd = sys.stdin.fileno()
    settings = termios.tcgetattr(fd)
    animation_status = 0
    try:
      sys.stdout.write("\033[?1049h\033[?25l\033[0m\033[40m\033[2J")
      sys.stdout.flush()
      tty.setcbreak(fd)
      mode = termios.tcgetattr(fd)
      mode[1] |= termios.OPOST | termios.ONLCR
      termios.tcsetattr(fd, termios.TCSANOW, mode)
      while task.poll() is None and not dismissed and not cancelled:
        resized = False
        size = os.get_terminal_size(fd)
        columns, rows = max(1, size.columns - 1), max(1, size.lines - 1)
        sys.stdout.write(f"\033[2J\033[{rows + 1};1H\0337")
        sys.stdout.flush()
        env = dict(os.environ, COLUMNS=str(columns), LINES=str(rows))
        animation = subprocess.Popen([
          "ttfx", "-i", str(art), "--frame-rate", "120",
          "--canvas-width", "0", "--canvas-height", "0", "--reuse-canvas",
          "--anchor-canvas", "c", "--anchor-text", "c", "--random-effect",
          "--no-eol", "--no-restore-cursor",
        ], stdin=subprocess.DEVNULL, env=env)
        while task.poll() is None and animation.poll() is None and not cancelled:
          if select.select([fd], [], [], 0.05)[0] and os.read(fd, 64):
            dismissed = True
            break
        if not resized and not dismissed and not cancelled and task.poll() is None and animation.poll() not in (None, 0):
          animation_status = animation.returncode
          break
        if animation.poll() is None:
          animation.terminate()
        animation.wait()
    except (OSError, subprocess.SubprocessError) as error:
      print(f"Could not display screensaver: {error}", file=sys.stderr)
      animation_status = 1
    finally:
      if animation is not None and animation.poll() is None:
        animation.terminate()
        animation.wait()
      termios.tcsetattr(fd, termios.TCSADRAIN, settings)
      sys.stdout.write("\033[?25h\033[?1049l\033[0m")
      sys.stdout.flush()

    try:
      if cancelled or animation_status:
        cancel_task(task)
      # A key dismisses the animation, not the command. Replay then follow its log.
      with log_path.open("rb") as log:
        while True:
          if cancelled and task.poll() is None:
            cancel_task(task)
          chunk = log.read(65536)
          if chunk:
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
          elif task.poll() is not None:
            break
          else:
            time.sleep(0.05)
      status = task.wait()
      if cancelled:
        return 128 + cancelled
      return animation_status or (status if status >= 0 else 128 - status)
    finally:
      if task.poll() is None:
        cancel_task(task)
      print(f"\nTask log: {log_path}", file=sys.stderr)


if __name__ == "__main__":
  raise SystemExit(run(sys.argv[1:]))
