"""Create (or update) what the coding jobs of JOBS_RUNNER=managed need on the Anthropic
side, once (docs/jobs.md). Needs `pip install anthropic pyyaml`.

    ANTHROPIC_API_KEY=<your key> python scripts/jobs_setup.py --github-token <token>

It applies agents/coder.md (the coding subagent: YAML front matter for its configuration,
Markdown body for its system prompt) and environments/coder.yaml (its sandbox), creates a
vault holding the GitHub token as GH_TOKEN for GitHub's API, and writes the JOBS_* lines
for the server's .env to jobs.env (chmod 600). Run it again after editing agents/coder.md
with --agent-id to publish a new version of the agent; pass --environment-id and
--vault-id to keep the existing ones. Same thing as `ant apply`, without the CLI.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENT_FILE = ROOT / "agents" / "coder.md"
ENVIRONMENT_FILE = ROOT / "environments" / "coder.yaml"
GITHUB_HOSTS = ["api.github.com"]


def load_agent(path: Path = AGENT_FILE) -> dict[str, Any]:
    """The agent's create/update body: front matter fields plus the body as ``system``."""
    text = path.read_text()
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"{path} needs a YAML front matter between --- lines.")
    end = text.index("\n---\n", 4)
    config = yaml.safe_load(text[4:end]) or {}
    config["system"] = text[end + 5 :].strip()
    return config


def load_environment(path: Path = ENVIRONMENT_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github-token", default=os.environ.get("JOBS_GITHUB_TOKEN"))
    parser.add_argument("--agent-id", help="Update this agent instead of creating one.")
    parser.add_argument("--environment-id", help="Keep this environment.")
    parser.add_argument("--vault-id", help="Keep this vault (its GH_TOKEN is left as is).")
    parser.add_argument("--out", type=Path, default=Path("jobs.env"))
    args = parser.parse_args()

    import anthropic

    client = anthropic.Anthropic()
    agent_body = load_agent()
    if args.agent_id:
        agent = client.beta.agents.update(args.agent_id, **agent_body)
        print(f"Agent {agent.id} updated to version {agent.version}")
    else:
        agent = client.beta.agents.create(**agent_body)
        print(f"Agent {agent.id} created (version {agent.version})")

    environment_id = args.environment_id
    if not environment_id:
        environment = client.beta.environments.create(**load_environment())
        environment_id = environment.id
        print(f"Environment {environment_id} created")

    vault_id = args.vault_id
    if not vault_id and args.github_token:
        vault = client.beta.vaults.create(display_name="intelligent-home-agent coder")
        client.beta.vaults.credentials.create(
            vault.id,
            display_name="GitHub token for the coder",
            auth={
                "type": "environment_variable",
                "secret_name": "GH_TOKEN",
                "secret_value": args.github_token,
                "networking": {"type": "limited", "allowed_hosts": GITHUB_HOSTS},
                "injection_location": {"header": True},
            },
        )
        vault_id = vault.id
        print(f"Vault {vault_id} created with GH_TOKEN for {', '.join(GITHUB_HOSTS)}")
    elif not vault_id:
        print("No --github-token: no vault, the coder can push but not call GitHub's API.")

    lines = [f"JOBS_AGENT_ID={agent.id}", f"JOBS_ENVIRONMENT_ID={environment_id}"]
    if vault_id:
        lines.append(f"JOBS_VAULT_ID={vault_id}")
    if args.github_token:
        lines.append(f"JOBS_GITHUB_TOKEN={args.github_token}")
    args.out.write_text("\n".join(lines) + "\n")
    os.chmod(args.out, 0o600)
    print(f"\nWrote {args.out}: add its lines to the server's .env, then delete it.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
