import json

import httpx
import pytest
import respx

from intelligent_home_agent.tools import ToolError
from intelligent_home_agent.tools.repos import Repositories
from intelligent_home_agent.tools.vulnerabilities import (
    OSV_URL,
    Vulnerabilities,
    build_vulnerability_tools,
    lockfile_kind,
)

GITHUB = "https://api.github.test"
NPM_LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {"name": "app"},
        "node_modules/lodash": {"version": "4.17.20"},
        "node_modules/left-pad": {"version": "1.3.0"},
    },
}


def test_lockfiles_are_found_but_not_in_vendored_folders():
    assert lockfile_kind("web/package-lock.json")[0] == "npm"
    assert lockfile_kind("requirements-dev.txt")[0] == "PyPI"
    assert lockfile_kind("composer.lock")[0] == "Packagist"
    assert lockfile_kind("node_modules/x/package-lock.json") is None
    assert lockfile_kind("package.json") is None


@respx.mock
async def test_flaws_come_most_serious_first_with_their_fix(ctx):
    respx.get(f"{GITHUB}/user/repos").respond(json=[{"full_name": "me/app"}])
    respx.get(f"{GITHUB}/repos/me/app/git/trees/HEAD").respond(
        json={
            "tree": [
                {"path": "package-lock.json", "type": "blob"},
                {"path": "api/requirements.txt", "type": "blob"},
                {"path": "node_modules/x/package-lock.json", "type": "blob"},
            ]
        }
    )
    respx.get(f"{GITHUB}/repos/me/app/contents/package-lock.json").respond(
        text=json.dumps(NPM_LOCK)
    )
    respx.get(f"{GITHUB}/repos/me/app/contents/api/requirements.txt").respond(
        text="fastapi==0.110.0\nhttpx>=0.27  # not pinned\n"
    )
    batch = respx.post(f"{OSV_URL}/querybatch").respond(
        json={"results": [{"vulns": [{"id": "MAL-1"}]}, {}, {"vulns": [{"id": "GHSA-a"}]}]}
    )
    respx.get(f"{OSV_URL}/vulns/MAL-1").respond(json={"id": "MAL-1", "summary": "Malicious code"})
    respx.get(f"{OSV_URL}/vulns/GHSA-a").respond(
        json={
            "id": "GHSA-a",
            "summary": "Prototype pollution",
            "database_specific": {"severity": "HIGH"},
            "affected": [
                {
                    "package": {"name": "lodash"},
                    "ranges": [{"events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}],
                }
            ],
        }
    )
    async with httpx.AsyncClient() as http:
        vulns = Vulnerabilities(http, Repositories(http, "t", "me/config", api_url=GITHUB))
        (tool,) = build_vulnerability_tools(vulns)
        result = await tool.handler({"repo": "me/app"}, ctx)

    queries = json.loads(batch.calls[0].request.content)["queries"]
    assert {(q["package"]["name"], q["version"]) for q in queries} == {
        ("fastapi", "0.110.0"),
        ("left-pad", "1.3.0"),
        ("lodash", "4.17.20"),
    }
    assert result["lockfiles"] == ["package-lock.json", "api/requirements.txt"]
    assert [(f["ids"][0], f["severity"]) for f in result["serious"]] == [
        ("MAL-1", "MALICIOUS"),
        ("GHSA-a", "HIGH"),
    ]
    assert result["serious"][1]["fixed_in"] == ["4.17.21"]
    assert result["others_by_severity"] == {} and result["not_detailed"] == 0
    assert tool.taints


def test_pnpm_lockfiles_are_read():
    from intelligent_home_agent.tools.vulnerabilities import _pnpm

    lock = (
        "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies: {}\npackages:\n"
        "  '@babel/core@7.29.7':\n    resolution: {}\n  lodash@4.17.21:\n    resolution: {}\n"
        "  react-dom@19.0.0(react@19.0.0):\n    resolution: {}\nsnapshots:\n  x@1.0.0: {}\n"
    )
    assert _pnpm(lock) == [
        ("@babel/core", "7.29.7"),
        ("lodash", "4.17.21"),
        ("react-dom", "19.0.0"),
    ]


@respx.mock
async def test_only_the_users_own_repos_are_checked(ctx):
    respx.get(f"{GITHUB}/user/repos").respond(json=[{"full_name": "me/app"}])
    async with httpx.AsyncClient() as http:
        (tool,) = build_vulnerability_tools(
            Vulnerabilities(http, Repositories(http, "t", "me/config", api_url=GITHUB))
        )
        with pytest.raises(ToolError, match="not one of the user's repositories"):
            await tool.handler({"repo": "someone/huge-monorepo"}, ctx)


@respx.mock
async def test_a_file_past_the_cap_stops_downloading(ctx):
    respx.get(f"{GITHUB}/repos/me/app/contents/package-lock.json").respond(content=b"x" * 5000)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ToolError, match="too big"):
            await Repositories(http, "t", "me/config", api_url=GITHUB).raw(
                "me/app", "package-lock.json", 1000
            )


async def test_a_bad_repo_name_is_refused(ctx):
    async with httpx.AsyncClient() as http:
        (tool,) = build_vulnerability_tools(
            Vulnerabilities(http, Repositories(http, "t", "me/config", api_url=GITHUB))
        )
        with pytest.raises(ToolError, match="owner/name"):
            await tool.handler({"repo": "../../etc"}, ctx)
