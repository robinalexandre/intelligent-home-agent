"""Known flaws in the dependencies of the user's repositories, from OSV.dev.

The lockfiles of a repository give the exact versions it runs (npm, Composer, Poetry or
uv, Pipfile, pinned requirements); OSV.dev, the open vulnerability database, says which
of them have a published flaw, how serious, and from which version it is fixed. It also
lists malicious packages (ids starting with MAL-): a dependency that was compromised.
What it returns was written by others: it taints the request. The names and versions of
the packages are sent to OSV.dev, nothing else of the repository.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from collections import Counter
from typing import Any

import httpx

from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError
from intelligent_home_agent.tools.repos import Repositories

OSV_URL = "https://api.osv.dev/v1"
MAX_LOCKFILES = 10
MAX_DETAILS = 400
DETAILS_AT_ONCE = 10
SERIOUS = {"MALICIOUS", "CRITICAL", "HIGH"}
MAX_SERIOUS = 40
MAX_LOCKFILE_BYTES = 20 * 1024 * 1024
_SKIPPED = ("node_modules/", "vendor/", ".venv/", "site-packages/")
_PIN = re.compile(r"^([A-Za-z0-9_.\-\[\]]+?)(?:\[[^\]]*\])?==([^\s;#]+)")
_SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "MEDIUM": 2, "LOW": 3}


def _npm(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    found = []
    for path, info in (data.get("packages") or {}).items():
        if path and info.get("version") and not info.get("link"):
            found.append((path.rsplit("node_modules/", 1)[-1], info["version"]))
    for name, info in (data.get("dependencies") or {}).items():  # lockfile v1
        if info.get("version"):
            found.append((name, info["version"]))
    return found


def _composer(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    return [
        (p["name"], str(p["version"]).lstrip("v"))
        for p in [*(data.get("packages") or []), *(data.get("packages-dev") or [])]
        if p.get("name") and p.get("version")
    ]


def _toml_lock(text: str) -> list[tuple[str, str]]:
    return [
        (p["name"], p["version"]) for p in tomllib.loads(text).get("package", []) if "version" in p
    ]


def _pipfile(text: str) -> list[tuple[str, str]]:
    data = json.loads(text)
    return [
        (name, info["version"].lstrip("="))
        for section in ("default", "develop")
        for name, info in (data.get(section) or {}).items()
        if str(info.get("version", "")).startswith("==")
    ]


_PNPM_KEY = re.compile(r"^  '?/?(@?[^@\s'/][^@\s']*)@([^('\s:]+)")


def _pnpm(text: str) -> list[tuple[str, str]]:
    """The packages section of pnpm-lock.yaml, keys like '@babel/core@7.29.7':."""
    found, inside = [], False
    for line in text.splitlines():
        if not line.strip():
            continue  # blank lines separate the entries
        if not line.startswith(" "):
            inside = line.strip() == "packages:"
            continue
        if inside and (match := _PNPM_KEY.match(line)):
            found.append((match[1], match[2]))
    return found


def _requirements(text: str) -> list[tuple[str, str]]:
    return [(m[1], m[2]) for line in text.splitlines() if (m := _PIN.match(line.strip()))]


# Lockfile name -> OSV ecosystem and how to read it.
PARSERS: dict[str, tuple[str, Any]] = {
    "package-lock.json": ("npm", _npm),
    "pnpm-lock.yaml": ("npm", _pnpm),
    "composer.lock": ("Packagist", _composer),
    "poetry.lock": ("PyPI", _toml_lock),
    "uv.lock": ("PyPI", _toml_lock),
    "Pipfile.lock": ("PyPI", _pipfile),
}


def lockfile_kind(path: str) -> tuple[str, Any] | None:
    name = path.rsplit("/", 1)[-1]
    if any(part in path for part in _SKIPPED):
        return None
    if name in PARSERS:
        return PARSERS[name]
    if name.startswith("requirements") and name.endswith(".txt"):
        return ("PyPI", _requirements)
    return None


def _severity(vuln: dict[str, Any]) -> str:
    if vuln.get("id", "").startswith("MAL-"):
        return "MALICIOUS"
    level = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    return level or "UNKNOWN"


def _fixed(vuln: dict[str, Any], name: str) -> list[str]:
    return sorted(
        {
            event["fixed"]
            for affected in vuln.get("affected", [])
            if (affected.get("package") or {}).get("name", "").casefold() == name.casefold()
            for r in affected.get("ranges", [])
            for event in r.get("events", [])
            if "fixed" in event
        }
    )


class Vulnerabilities:
    def __init__(self, http: httpx.AsyncClient, repos: Repositories, osv_url: str = OSV_URL):
        self._http = http
        self.repos = self._repos = repos
        self._osv = osv_url.rstrip("/")

    async def _osv_call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            resp = await self._http.request(method, f"{self._osv}{path}", json=body, timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"OSV.dev unavailable: {exc}") from exc
        return resp.json()

    async def check(self, repo: str) -> dict[str, Any]:
        tree = await self._repos.tree(repo)
        lockfiles = [(p, kind) for p in tree if (kind := lockfile_kind(p))][:MAX_LOCKFILES]
        packages: dict[tuple[str, str, str], set[str]] = {}
        unreadable = []
        for path, (ecosystem, parse) in lockfiles:
            try:
                text = await self._repos.raw(repo, path, MAX_LOCKFILE_BYTES)
                for name, version in parse(text):
                    packages.setdefault((ecosystem, name, version), set()).add(path)
            except (ValueError, KeyError, TypeError, ToolError):
                unreadable.append(path)
        keys = sorted(packages)
        found: list[tuple[tuple[str, str, str], str]] = []
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            data = await self._osv_call(
                "POST",
                "/querybatch",
                {
                    "queries": [
                        {"package": {"ecosystem": e, "name": n}, "version": v} for e, n, v in batch
                    ]
                },
            )
            for key, result in zip(batch, data.get("results", []), strict=False):
                found += [(key, v["id"]) for v in result.get("vulns") or []]
        # Every flaw's details, a few at a time: a serious one must never stay unknown.
        details: dict[str, dict[str, Any]] = {}
        ids = list(dict.fromkeys(v for _, v in found))[:MAX_DETAILS]
        for start in range(0, len(ids), DETAILS_AT_ONCE):
            chunk = ids[start : start + DETAILS_AT_ONCE]
            answers = await asyncio.gather(*(self._osv_call("GET", f"/vulns/{i}") for i in chunk))
            details.update(zip(chunk, answers, strict=True))
        flaws = []
        for (ecosystem, name, version), vuln_id in found:
            vuln = details.get(vuln_id, {"id": vuln_id})
            flaws.append(
                {
                    "package": name,
                    "ecosystem": ecosystem,
                    "version": version,
                    "in": sorted(packages[(ecosystem, name, version)]),
                    "id": vuln_id,
                    "aliases": vuln.get("aliases", [])[:3],
                    "severity": _severity(vuln),
                    "summary": str(vuln.get("summary") or "")[:200],
                    "fixed_in": _fixed(vuln, name)[:3],
                }
            )
        flaws.sort(
            key=lambda f: _SEVERITY_ORDER.get(
                f["severity"], -1 if f["severity"] == "MALICIOUS" else 4
            )
        )
        # One line per package and version, its worst flaw first: what to upgrade, to what.
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for flaw in (f for f in flaws if f["severity"] in SERIOUS):
            key = (flaw["ecosystem"], flaw["package"], flaw["version"])
            entry = grouped.setdefault(key, {**flaw, "ids": [], "fixed_in": []})
            entry["ids"].append(flaw["id"])
            entry["fixed_in"] = sorted({*entry["fixed_in"], *flaw["fixed_in"]})
        serious = [
            {k: v for k, v in g.items() if k not in {"id", "aliases"}} | {"ids": g["ids"][:5]}
            for g in list(grouped.values())[:MAX_SERIOUS]
        ]
        return {
            "repo": repo,
            "lockfiles": [p for p, _ in lockfiles],
            "unreadable": unreadable,
            "packages_checked": len(keys),
            "serious": serious,
            "others_by_severity": dict(
                Counter(f["severity"] for f in flaws if f["severity"] not in SERIOUS)
            ),
            "not_detailed": len(set(v for _, v in found)) - len(details),
        }


def build_vulnerability_tools(vulns: Vulnerabilities | None) -> list[Tool]:
    if vulns is None:
        return []

    async def check_dependencies(args: dict[str, Any], _: ToolContext) -> dict[str, Any]:
        repo = str(args.get("repo") or "").strip()
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ToolError('repo is "owner/name", from list_repositories.')
        # Only the user's own repositories: not any public one a text could point to.
        if repo.casefold() not in {r["repo"].casefold() for r in await vulns.repos.owned()}:
            raise ToolError(f"{repo} is not one of the user's repositories.")
        return await vulns.check(repo)

    return [
        Tool(
            name="check_dependencies",
            description=(
                "Known flaws in the exact versions a repository depends on (its lockfiles: "
                "npm, pnpm, Composer, Poetry, uv, Pipfile, pinned requirements), from OSV.dev: "
                "the critical, high and malicious ones in full with the version that fixes "
                "each, the others counted. MALICIOUS means a compromised package. Repos from "
                "list_repositories."
            ),
            input_schema={
                "type": "object",
                "properties": {"repo": {"type": "string", "description": '"owner/name"'}},
                "required": ["repo"],
            },
            handler=check_dependencies,
            taints=True,
        )
    ]
