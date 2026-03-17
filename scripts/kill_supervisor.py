#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "supervisor",
# ]
# ///

"""
Script for killing `supervisor`

This script is designed as an event listener for `supervisor`,
following the specifications outlined in its documentation related to
[event notifications](https://supervisord.org/events.html).

**Purpose:**

Supervisor does not automatically stop when managed processes die or stop.
This script addresses that gap by monitoring state changes of managed processes
and gracefully shutting down `supervisor` when specific events occur.
The script is run as a subprocess by `supervisor`, continuously listening for relevant event notifications.

**How It Works:**

1. **Event Listener Initialization**: `supervisor` launches this script based on its configuration.
2. **Event Interception**: It handles the following specific events from `supervisor`:
   - [`PROCESS_STATE_EXITED`](https://supervisord.org/events.html#process-state-exited-event-type):
     Process unexpectedly exits the `RUNNING` state.
   - [`PROCESS_STATE_FATAL`](https://supervisord.org/events.html#process-state-fatal-event-type):
     Process repeatedly fails to start and enters a `FATAL` state.
3. **Process Monitoring**:
   - When one of the monitored processes transitions to any of the above states,
     the script takes action to stop the `supervisor` process.
   - It uses the process ID (PID) from the `supervisord.pid` file.
4. **Communication with Supervisor**:
   - The script reads structured input (events) sent to it on standard input by `supervisor`.
   - It communicates results back to `supervisor` in the format that it expects.

**Configuration:**

Here is an example configuration from the `supervisord.conf` file:

```ini
[supervisord] ; Supervisor's own configuration details
; Configuration properties go here

[program:server] ; Configuration for a process to be monitored
process_name=server
; Remaining configuration properties go here

[eventlistener:server-exit]
command=/usr/local/bin/kill-supervisor --watch=server --pidfile=/tmp/supervisord.pid
events=PROCESS_STATE_EXITED,PROCESS_STATE_FATAL
autorestart=false
startsecs=0
startretries=0
```
"""

from argparse import ArgumentParser, Namespace
from os import kill
from pathlib import Path
from signal import SIGKILL
from sys import stderr

from supervisor.childutils import get_headers, listener


def watch(
    process: str,
    pidfile: Path,
):
    while True:
        # Transition from `ACKNOWLEDGED` state to `READY`,
        # wait for a header to arrive and read it,
        # then use this information to read the payload.
        headers, raw = listener.wait()
        print(f"Received payload: {raw}", file=stderr, flush=True)
        payload = get_headers(raw)

        # We proceed with the kill logic if the event process matches the one from the command argument.
        # Since we only observe process state changes to non-running states (EXITED or FATAL),
        # this means that the observed process is not running.
        if process == payload.get("processname"):
            print("Process event detected, shutting down `supervisord`...", file=stderr, flush=True)
            try:
                buffer = open(file=pidfile, mode="r")
                pid = int(buffer.readline().strip())
                kill(pid, SIGKILL)
                print(f"Sent SIGKILL to `supervisord` (PID: {pid})", file=stderr, flush=True)
            except FileNotFoundError as ex:
                print(f"{ex.strerror}: {ex.filename}", file=stderr, flush=True)
                listener.fail()
                exit(1)
            except Exception as ex:
                name = ex.__class__.__name__
                detail = f"{name}: {detail}" if (detail := str(ex)) else name
                print(f"Could not kill supervisor: {detail}", file=stderr, flush=True)

        # Transition back to `READY` state from `ACKNOWLEDGED`
        listener.ok()


def main(args: Namespace):
    process: str = args.watch
    pidfile: Path = args.pidfile
    watch(
        process=process,
        pidfile=pidfile,
    )


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="kill-supervisor",
        description=(
            "Kill `supervisor` when another process receives an event. "
            "Intended for use within `eventlistener` sections of `supervisord.conf`."
        ),
        epilog="To specify the value for a flag, you can either use --flag=[VALUE] or --flag [VALUE]",
    )
    parser.add_argument(
        "-w",
        "--watch",
        help="Name of the process to observe events for",
        required=True,
    )
    parser.add_argument(
        "-p",
        "--pidfile",
        help="Path to the `supervisord.pid` file",
        default=Path("/var/run/supervisord.pid"),
        type=Path,
        nargs="?",
    )
    main(args=parser.parse_args())
