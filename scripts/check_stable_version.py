#!/usr/bin/env python3
"""Fail closed unless this skill and selected scoring standard match its latest stable GitHub release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
SEMVER = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
EXCLUDED_DIRS = {
    ".git", "tmp", "dist", "node_modules", "output", "__pycache__",
    "ocr-shims", "实验报告_测试样本", ".workbuddy",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


class VersionGateError(RuntimeError):
    """A mandatory online stable-version check could not be completed."""


def _repo_slug(skill_root: Path) -> str:
    manifest_path = skill_root / "skill.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        repository = str(manifest.get("repository", ""))
    except (OSError, json.JSONDecodeError) as error:
        raise VersionGateError(f"无法读取 Skill 仓库信息 {manifest_path}: {error}") from error
    parsed = urlparse(repository)
    if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com":
        raise VersionGateError("skill.json 必须配置 https://github.com/<owner>/<repo> 仓库地址")
    parts = [part for part in parsed.path.strip("/").removesuffix(".git").split("/") if part]
    if len(parts) != 2:
        raise VersionGateError(f"无法从仓库地址解析 owner/repo：{repository}")
    return "/".join(parts)


def _get_json(url: str) -> tuple[dict | list, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "experiment-report-grader-version-gate",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload, response.headers.get("Link", "")
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VersionGateError(
            f"无法在线核验 GitHub 稳定版本（{url}）：{error}。按强制版本门槛停止评分，不使用缓存或 main。"
        ) from error


def _get_pages(url: str) -> list[dict]:
    items: list[dict] = []
    pages = 0
    while url:
        payload, link_header = _get_json(url)
        if not isinstance(payload, list):
            raise VersionGateError(f"GitHub API 返回了非列表数据：{url}")
        items.extend(item for item in payload if isinstance(item, dict))
        pages += 1
        if pages >= 20:
            raise VersionGateError("GitHub 稳定版本列表超过 20 页，无法完整核验；停止评分。")
        match = LINK_NEXT.search(link_header)
        url = match.group(1) if match else ""
    return items


def _stable_tuple(value: str) -> tuple[int, int, int] | None:
    match = SEMVER.fullmatch(value.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def _latest_stable_tag(repo: str) -> tuple[str, tuple[int, int, int], str]:
    releases = _get_pages(f"{API_ROOT}/repos/{repo}/releases?per_page=100")
    candidates: dict[tuple[int, int, int], set[str]] = {}
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name", ""))
        version = _stable_tuple(tag)
        if version:
            candidates.setdefault(version, set()).add(tag)
    if not candidates:
        raise VersionGateError(
            "GitHub 仓库没有已发布的正式稳定 Release（tag 要求 vMAJOR.MINOR.PATCH）；"
            "当前不能正式评分，也不会用 main 或缓存版本替代。"
        )
    version = max(candidates)
    names = sorted(candidates[version])
    if len(names) != 1:
        raise VersionGateError(f"最新稳定版本号 {version} 对应多个正式 Release tag，无法唯一确认：{names}")
    tag = names[0]
    return tag, version, "published stable release"


def _git_commit(skill_root: Path) -> str | None:
    if not (skill_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(skill_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VersionGateError(f"本地存在 Git 元数据，但无法核验当前 commit：{error}") from error
    return result.stdout.strip()


def _is_local_only(relative: str) -> bool:
    path = Path(relative)
    parts = path.parts
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    if relative == "config/local.json" or path.name == ".DS_Store" or path.suffix.casefold() in EXCLUDED_SUFFIXES:
        return True
    if len(parts) >= 2 and parts[:2] == ("assets", "tessdata"):
        return True
    if len(parts) >= 2 and parts[:2] == ("assets", "signature") and path.suffix.casefold() in {".pdf", ".png", ".jpg", ".jpeg"}:
        return True
    return False


def _git_blob_sha(data: bytes) -> str:
    # .gitattributes stores text with LF on every supported host; binary assets contain NULs.
    if b"\0" not in data:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _verify_tree(skill_root: Path, remote_tree: dict[str, str], rubric_relative: str) -> None:
    if rubric_relative not in remote_tree:
        raise VersionGateError(f"所选评分标准不在最新稳定版本中：{rubric_relative}；停止评分。")
    missing: list[str] = []
    changed: list[str] = []
    extra: list[str] = []
    for relative, expected_sha in remote_tree.items():
        if _is_local_only(relative):
            continue
        local_path = skill_root / Path(relative)
        if not local_path.is_file():
            missing.append(relative)
            continue
        try:
            actual_sha = _git_blob_sha(local_path.read_bytes())
        except OSError as error:
            raise VersionGateError(f"无法读取稳定版本核验文件 {relative}: {error}") from error
        if actual_sha != expected_sha:
            changed.append(relative)
    for local_path in skill_root.rglob("*"):
        if not local_path.is_file():
            continue
        relative = local_path.relative_to(skill_root).as_posix()
        if not _is_local_only(relative) and relative not in remote_tree:
            extra.append(relative)
    if missing or changed or extra:
        details = []
        if missing:
            details.append(f"缺少文件 {missing[:5]}")
        if changed:
            details.append(f"与稳定版内容不同 {changed[:8]}")
        if extra:
            details.append(f"存在未发布文件 {extra[:8]}")
        raise VersionGateError(
            "本机 Skill 文件树与最新稳定 tag 不一致（" + "；".join(details) + "）；"
            "请先安装该稳定版本或发布并安装获批版本，再评分。"
        )


def verify_stable_version(
    skill_root: Path,
    rubric_path: Path,
    rubric_id: str = "",
    rubric_sha256: str = "",
) -> dict:
    """Check network freshness, exact release commit/tree, skill metadata, and rubric bytes."""
    skill_root = skill_root.resolve()
    rubric_path = rubric_path.resolve()
    try:
        rubric_relative = rubric_path.relative_to(skill_root).as_posix()
    except ValueError as error:
        raise VersionGateError(
            "所选评分标准位于 Skill 仓库之外，无法与稳定发布版核对；请先将其纳入仓库并发布稳定版本。"
        ) from error
    try:
        manifest = json.loads((skill_root / "skill.json").read_text(encoding="utf-8"))
        rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
        rubric_raw_sha256 = hashlib.sha256(rubric_path.read_bytes()).hexdigest()
    except (OSError, json.JSONDecodeError) as error:
        raise VersionGateError(f"无法读取 Skill 版本或评分标准文件：{error}") from error

    repo = _repo_slug(skill_root)
    tag, version_tuple, source = _latest_stable_tag(repo)
    local_skill_version = str(manifest.get("skill_version", ""))
    if _stable_tuple(local_skill_version) != version_tuple:
        raise VersionGateError(
            f"本机 Skill 版本 {local_skill_version or '未记录'} 与 GitHub 最新稳定版本 {tag} 不一致；停止评分。"
        )

    commit_payload, _ = _get_json(f"{API_ROOT}/repos/{repo}/commits/{quote(tag, safe='')}")
    commit_sha = str(commit_payload.get("sha", "")) if isinstance(commit_payload, dict) else ""
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise VersionGateError(f"无法解析稳定 tag {tag} 对应的 commit；停止评分。")
    local_commit = _git_commit(skill_root)
    if local_commit and local_commit.casefold() != commit_sha.casefold():
        raise VersionGateError(
            f"本机 commit {local_commit} 与最新稳定 tag {tag} 的 commit {commit_sha} 不一致；停止评分。"
        )

    tree_payload, _ = _get_json(f"{API_ROOT}/repos/{repo}/git/trees/{commit_sha}?recursive=1")
    if not isinstance(tree_payload, dict) or tree_payload.get("truncated"):
        raise VersionGateError("GitHub 返回的稳定版本文件树不完整，无法完成比对；停止评分。")
    tree = tree_payload.get("tree")
    if not isinstance(tree, list):
        raise VersionGateError("GitHub 稳定版本没有可读取的文件树；停止评分。")
    remote_blobs = {
        str(item["path"]): str(item["sha"])
        for item in tree
        if isinstance(item, dict) and item.get("type") == "blob" and item.get("path") and item.get("sha")
    }
    if not remote_blobs:
        raise VersionGateError("GitHub 稳定版本文件树为空；停止评分。")
    _verify_tree(skill_root, remote_blobs, rubric_relative)
    if remote_blobs[rubric_relative] != _git_blob_sha(rubric_path.read_bytes()):
        raise VersionGateError("本地评分标准文件与稳定 tag 内容不一致；停止评分。")

    stable_rubric_id = str(rubric.get("rubric_id") or rubric.get("id") or "")
    if rubric_id and stable_rubric_id and rubric_id != stable_rubric_id:
        raise VersionGateError(
            f"评分标准标识不一致：评分流程读取 {rubric_id}，稳定文件记录 {stable_rubric_id}；停止评分。"
        )
    return {
        "status": "passed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "repository": f"https://github.com/{repo}",
        "stable_tag": tag,
        "stable_source": source,
        "stable_commit": commit_sha,
        "local_commit": local_commit,
        "skill_version": local_skill_version,
        "pipeline_version": str(manifest.get("pipeline_version", "")),
        "rubric_id": stable_rubric_id,
        "rubric_path": rubric_relative,
        "rubric_file_sha256": rubric_raw_sha256,
        "rubric_semantic_sha256": rubric_sha256,
        "verification": "exact commit and repository file tree" if local_commit else "complete repository file tree",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rubric", required=True, help="selected scoring-standard JSON file")
    parser.add_argument("--json", action="store_true", help="print the audit record as JSON")
    args = parser.parse_args()
    skill_root = Path(__file__).resolve().parent.parent
    rubric_path = Path(args.rubric).expanduser()
    if not rubric_path.is_absolute():
        rubric_path = skill_root / rubric_path
    try:
        record = verify_stable_version(skill_root, rubric_path)
    except VersionGateError as error:
        print(f"稳定版本核验失败：{error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(
            f"稳定版本核验通过：{record['stable_tag']} · commit {record['stable_commit']} · "
            f"评分标准 {record['rubric_id']} · SHA-256 {record['rubric_file_sha256']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
