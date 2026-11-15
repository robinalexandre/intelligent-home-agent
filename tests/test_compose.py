"""The Compose files, read as Compose merges them (COMPOSE_FILE lists several): what an
overlay names must exist in the set it is used with, and two files must give a service's
key in the same form."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = "docker-compose.yml"
OVERLAYS = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "compose").glob("*.yml"))
# The files an overlay only makes sense with, as its header says.
NEEDS = {
    "compose/browser-login.yml": ["compose/browser.yml"],
    "compose/browser-caddy.yml": [
        "compose/caddy.yml",
        "compose/browser.yml",
        "compose/browser-login.yml",
    ],
    "compose/browser-traefik.yml": [
        "compose/traefik.yml",
        "compose/browser.yml",
        "compose/browser-login.yml",
    ],
    "compose/vpn.yml": ["compose/browser.yml"],
}
# Everything the wizard can put together at once, per way of reaching the agent.
TOGETHER = [
    "compose/ollama.yml",
    "compose/searxng.yml",
    "compose/kb.yml",
    "compose/drive.yml",
    "compose/waha.yml",
    "compose/coder.yml",
]
FULL = {
    "tailscale": ["compose/tailscale.yml", "compose/browser.yml", "compose/vpn.yml"],
    "cloudflare": ["compose/cloudflared.yml", "compose/tailscale.yml"],
    "caddy": ["compose/caddy.yml", "compose/browser-caddy.yml", "compose/vpn.yml"],
    "traefik": ["compose/traefik.yml", "compose/browser-traefik.yml", "compose/vpn.yml"],
}
VARIABLE = re.compile(r"\$\{[A-Z0-9_]+(?::?[-?+][^}]*)?\}")


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text()) or {}


def with_needs(files: list[str]) -> list[str]:
    ordered: list[str] = [BASE]
    for name in files:
        for needed in [*NEEDS.get(name, []), name]:
            if needed not in ordered:
                ordered.append(needed)
    return ordered


def merged(files: list[str]) -> dict:
    """Mappings merge, lists add up, a scalar replaces: enough of Compose's merge to see
    what a set of files defines. Two forms of one key are an error, as in Compose."""

    def merge(old: dict, new: dict, where: str) -> dict:
        out = dict(old)
        for key, value in new.items():
            if key not in out:
                out[key] = value
            elif isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = merge(out[key], value, f"{where}.{key}")
            elif isinstance(out[key], list) and isinstance(value, list):
                out[key] = out[key] + [item for item in value if item not in out[key]]
            else:
                both = {type(out[key]), type(value)}
                assert not both & {dict, list} or len(both) == 1, (
                    f"{where}.{key} is a {type(out[key]).__name__} in one file and a "
                    f"{type(value).__name__} in another"
                )
                out[key] = value
        return out

    result: dict = {}
    for name in files:
        result = merge(result, load(name), name)
    return result


def names(value: object) -> list[str]:
    return list(value) if isinstance(value, (dict, list)) else []


def problems(files: list[str]) -> list[str]:
    stack = merged(files)
    services = stack.get("services") or {}
    networks, volumes = stack.get("networks") or {}, stack.get("volumes") or {}
    found = []
    for name, service in services.items():
        if not (service.get("image") or service.get("build")):
            found.append(f"{name}: no image and no build")
        for other in names(service.get("depends_on")):
            if other not in services:
                found.append(f"{name}: depends on {other}, which no file defines")
        for network in names(service.get("networks")):
            if network != "default" and network not in networks:
                found.append(f"{name}: network {network} is not declared")
        for volume in service.get("volumes") or []:
            if volume.startswith("./"):
                # A name chosen in .env (TS_SERVE, BROWSER_GATE): its default exists.
                path = VARIABLE.sub(lambda m: _default(m.group(0)), volume).split(":")[0]
                if not (ROOT / path).exists():
                    found.append(f"{name}: {path} is not in the repository")
                continue
            source = volume.split(":")[0]
            if not source.startswith(("/", "$")) and source not in volumes:
                found.append(f"{name}: volume {source} is not declared")
    return found


def _default(variable: str) -> str:
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:?-([^}]*)\}", variable)
    assert match, f"{variable} names a file and has no default"
    return match.group(1)


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_each_overlay_stands_on_the_base_with_the_files_it_needs(overlay):
    assert problems(with_needs([overlay])) == []


@pytest.mark.parametrize("access", sorted(FULL))
def test_everything_the_wizard_can_add_merges(access):
    assert problems(with_needs([*FULL[access], *TOGETHER])) == []


def test_fixed_subnets_are_used_once_and_ports_stay_on_this_machine():
    subnets: list[str] = []
    for name in [BASE, *OVERLAYS]:
        doc = load(name)
        for network in (doc.get("networks") or {}).values():
            for config in ((network or {}).get("ipam") or {}).get("config") or []:
                subnets.append(config["subnet"])
        for service_name, service in (doc.get("services") or {}).items():
            for port in service.get("ports") or []:
                # Only Caddy, which is there to be reached from outside, opens ports to
                # every interface; the agent's own follows AGENT_BIND, 127.0.0.1 unless set.
                local = port.startswith(("127.0.0.1:", "${AGENT_BIND:-127.0.0.1}:"))
                assert local or service_name == "caddy", f"{name}: {service_name} opens {port}"
    assert len(subnets) == len(set(subnets))


def test_the_files_a_setting_picks_exist():
    for name in ("serve", "funnel", "serve-browser"):
        assert (ROOT / f"compose/tailscale-{name}.json").exists()
    for name in ("login", "tailnet"):
        assert (ROOT / f"compose/browser/gate-{name}.Caddyfile").exists()
