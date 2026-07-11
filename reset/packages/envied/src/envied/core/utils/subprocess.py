import json
import subprocess
from pathlib import Path
from typing import Optional, Sequence, Union

from envied.core import binaries
from envied.core.console import console


def ffprobe(uri: Union[bytes, Path]) -> dict:
    """Use ffprobe on the provided data to get stream information."""
    if not binaries.FFProbe:
        raise EnvironmentError('FFProbe executable "ffprobe" not found but is required.')

    args = [binaries.FFProbe, "-v", "quiet", "-of", "json", "-show_streams"]
    if isinstance(uri, Path):
        args.extend(
            ["-f", "lavfi", "-i", "movie={}[out+subcc]".format(str(uri).replace("\\", "/").replace(":", "\\\\:"))]
        )
    elif isinstance(uri, bytes):
        args.append("pipe:")
    try:
        ff = subprocess.run(args, input=uri if isinstance(uri, bytes) else None, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        return {}
    return json.loads(ff.stdout.decode("utf8"))


def run_step(
    args: Sequence[Union[str, Path]],
    *,
    status: Optional[str] = None,
    output: Optional[Path] = None,
    label: str = "subprocess step",
) -> bytes:
    """Run a CLI step that writes to `output` (when provided). Returns stderr bytes.

    Raises RuntimeError with the stderr tail when the process exits non-zero, or when
    `output` is given and does not exist / is empty after the run.
    """
    if output is not None:
        output.unlink(missing_ok=True)

    str_args = [str(a) for a in args]
    if status:
        with console.status(status, spinner="dots"):
            p = subprocess.run(str_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    else:
        p = subprocess.run(str_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr = p.stderr or b""
    bad_output = output is not None and (not output.exists() or output.stat().st_size == 0)
    if p.returncode or bad_output:
        if output is not None:
            output.unlink(missing_ok=True)
        raise RuntimeError(f"{label} failed: {stderr.decode(errors='replace')[-400:]}")
    return stderr
