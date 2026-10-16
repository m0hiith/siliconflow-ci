"""Links into GitHub's web UI, built on the server so clients never guess
URL shapes. Pure functions, no I/O."""
from pathlib import PurePosixPath
from urllib.parse import quote


def _base(web_url: str, full_name: str) -> str:
    return f"{web_url.rstrip('/')}/{full_name}"


def commit_url(web_url: str, full_name: str, sha: str | None) -> str | None:
    return f"{_base(web_url, full_name)}/commit/{sha}" if sha else None


def run_url(web_url: str, full_name: str, run_id: int | None, attempt: int | None) -> str | None:
    if run_id is None:
        return None
    url = f"{_base(web_url, full_name)}/actions/runs/{run_id}"
    return f"{url}/attempts/{attempt}" if attempt else url


def compare_url(web_url: str, full_name: str, base: str, head: str) -> str:
    return f"{_base(web_url, full_name)}/compare/{base}...{head}"


def blob_url(web_url: str, full_name: str, sha: str, path: str, line: int | None) -> str:
    url = f"{_base(web_url, full_name)}/blob/{sha}/{quote(path)}"
    return f"{url}#L{line}" if line else url


def repo_relative(file: str, full_name: str) -> str | None:
    """A path printed by a tool in CI, relative to the repository root, or
    None when that cannot be known.

    Jobs check the repository out into the runner's workspace:
    /__w/<repo>/<repo> in a container job (firmware-ci.yml), or
    /home/runner/work/<repo>/<repo> on the host. A relative path is relative
    to whatever directory the tool ran in (the build or source dir), so it
    is not guessed at.
    """
    name = full_name.rsplit("/", 1)[-1]
    for workspace in (f"/__w/{name}/{name}/", f"/home/runner/work/{name}/{name}/"):
        if file.startswith(workspace):
            rest = file[len(workspace):]
            parts = PurePosixPath(rest).parts
            if rest and not any(p in ("", ".", "..") for p in parts) and "\\" not in rest:
                return "/".join(parts)
            return None
    return None
