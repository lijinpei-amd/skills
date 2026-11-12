"""Bound child commands and turn failed check groups into recorded failures."""

import json
import subprocess


def run(argv, *, timeout=45, **kwargs):
    """Capture a child result, including launch errors and timeouts."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(argv, 124, output,
                                           stderr + "\ncommand timed out after %s seconds" % timeout)
    except OSError as error:
        return subprocess.CompletedProcess(argv, 125, "", "cannot run command: %s" % error)


def failure_detail(result):
    """Use the child's last diagnostic, or preserve its failing exit status."""
    last = (result.stderr.strip().splitlines() or [""])[-1]
    return last or "command exited with status %s" % result.returncode


def validate_json(result, validate):
    """Validate a successful command's object response without aborting a suite."""
    if result.returncode != 0:
        return False, failure_detail(result)
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("JSON response must be an object")
        if "rows" in data and (not isinstance(data["rows"], list)
                               or any(not isinstance(row, dict) for row in data["rows"])):
            raise ValueError("JSON rows must be a list of objects")
        ok = bool(validate(data))
        return ok, "" if ok else "unexpected rows or counts"
    except Exception as error:
        return False, "%s: %s" % (type(error).__name__, error)


def protect(check, label, function, *args, **kwargs):
    """Record a failed check group and let its caller continue the suite."""
    try:
        return function(*args, **kwargs)
    except (Exception, SystemExit) as error:
        return check(label, False, "%s: %s" % (type(error).__name__, error))
