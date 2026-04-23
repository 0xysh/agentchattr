#!/usr/bin/env python3
"""Interactive macOS launcher for custom multi-agent rooms."""

from __future__ import annotations

import argparse
import colorsys
import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_config, resolve_config_path
from wrapper import _BUILTIN_DEFAULTS

DEFAULT_CONFIG_OUT = ROOT / "data" / "launcher.room.toml"
RESERVED_HANDLES = {"all", "both"}
HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _load_base_config() -> tuple[dict, Path]:
    config_path = resolve_config_path(ROOT)
    if not config_path.exists():
        raise FileNotFoundError(config_path)
    return load_config(ROOT, config_path=config_path), config_path


def _cli_providers(base_config: dict) -> list[str]:
    providers = []
    for name, agent_cfg in base_config.get("agents", {}).items():
        if not isinstance(agent_cfg, dict):
            continue
        if agent_cfg.get("type") == "api":
            continue
        if not agent_cfg.get("command"):
            continue
        providers.append(name)
    return providers


def _resolve_from_root(raw_path: str, fallback: str) -> str:
    path = Path(raw_path or fallback).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return str(path)


def _derive_color(base_hex: str, slot: int) -> str:
    """Create readable color variants for multiple instances of one provider."""
    if slot == 1:
        return base_hex

    hx = base_hex.lstrip("#")
    if len(hx) != 6:
        return base_hex

    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)

    magnitude = ((slot - 1 + 1) // 2) * 22
    direction = 1 if slot % 2 == 0 else -1
    h = (h + direction * magnitude / 360) % 1.0
    l = max(0.2, min(0.78, l + direction * 0.04))

    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return f"#{int(r2 * 255):02x}{int(g2 * 255):02x}{int(b2 * 255):02x}"


def _to_toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return json.dumps(str(value))


def _emit_section(lines: list[str], name: str, data: dict):
    lines.append(f"[{name}]")
    for key, value in data.items():
        lines.append(f"{key} = {_to_toml(value)}")
    lines.append("")


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _launch_in_terminal(command: str) -> bool:
    script = f'tell app "Terminal" to do script {json.dumps(command)}'
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _is_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _ensure_venv() -> Path:
    venv_dir = ROOT / ".venv"
    python_bin = venv_dir / "bin" / "python"
    pip_bin = venv_dir / "bin" / "pip"

    if not venv_dir.exists():
        print("Creating virtual environment...")
        subprocess.run(["python3", "-m", "venv", str(venv_dir)], check=True, cwd=ROOT)
        print("Installing Python dependencies...")
        subprocess.run([str(pip_bin), "install", "-q", "-r", "requirements.txt"], check=True, cwd=ROOT)

    return python_bin


def _ask_int(prompt: str, default: int = 0) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit():
            return int(raw)
        print("  Enter a whole number (0, 1, 2, ...).")


def _ask_handle(provider: str, index: int, default: str, used: set[str]) -> str:
    while True:
        raw = input(f"Handle for {provider} #{index} (used as @mention) [{default}]: ").strip().lower()
        handle = raw or default

        if handle in used:
            print("  That handle is already used. Pick another.")
            continue
        if handle in RESERVED_HANDLES:
            print(f"  '{handle}' is reserved. Pick another.")
            continue
        if not HANDLE_RE.match(handle):
            print("  Use lowercase letters, digits, hyphens; start with a letter; 2-32 chars.")
            continue
        return handle


def _ask_project_dir(default_dir: Path) -> str:
    while True:
        raw = input(f"Project folder for this room (all agents run here) [{default_dir}]: ").strip()
        if not raw:
            chosen = default_dir
        else:
            chosen = Path(raw).expanduser()
            if not chosen.is_absolute():
                chosen = (ROOT / chosen).resolve()

        if not chosen.exists() or not chosen.is_dir():
            print("  That folder does not exist. Enter an existing directory path.")
            continue

        return str(chosen)


def _default_project_dir(base_config: dict, providers: list[str]) -> Path:
    base_agents = dict(base_config.get("agents", {}))
    for provider in providers:
        provider_cfg = base_agents.get(provider, {})
        if isinstance(provider_cfg, dict) and provider_cfg.get("cwd"):
            return Path(_resolve_from_root(provider_cfg.get("cwd", ".."), ".."))
    return (ROOT / "..").resolve()


def _provider_template(base_agents: dict, provider: str, project_cwd: str, slot: int, handle: str) -> dict:
    provider_cfg = dict(base_agents.get(provider, {}))
    template = {}
    template.update(_BUILTIN_DEFAULTS.get(provider, {}))
    template.update(provider_cfg)
    template.pop("type", None)
    template["command"] = provider_cfg.get("command", provider)
    template["cwd"] = project_cwd
    template["label"] = handle
    template["color"] = _derive_color(provider_cfg.get("color", "#888888"), slot)
    return template


def _build_config(
    base: dict,
    providers: list[str],
    handles_by_provider: dict[str, list[str]],
    output_path: Path,
    project_cwd: str,
) -> tuple[str, int]:
    server_cfg = dict(base.get("server", {}))
    routing_cfg = dict(base.get("routing", {}))
    mcp_cfg = dict(base.get("mcp", {}))
    images_cfg = dict(base.get("images", {}))
    base_agents = dict(base.get("agents", {}))

    server_cfg.setdefault("port", 8300)
    server_cfg.setdefault("host", "127.0.0.1")
    server_cfg["data_dir"] = _resolve_from_root(server_cfg.get("data_dir", "./data"), "./data")

    routing_cfg.setdefault("default", "none")
    routing_cfg.setdefault("max_agent_hops", 4)

    mcp_cfg.setdefault("http_port", 8200)
    mcp_cfg.setdefault("sse_port", 8201)

    images_cfg["upload_dir"] = _resolve_from_root(images_cfg.get("upload_dir", "./uploads"), "./uploads")
    images_cfg.setdefault("max_size_mb", 10)

    agents_out: list[tuple[str, dict]] = []
    for provider in providers:
        for idx, handle in enumerate(handles_by_provider[provider], start=1):
            agents_out.append((handle, _provider_template(base_agents, provider, project_cwd, idx, handle)))

    lines: list[str] = []
    _emit_section(lines, "server", server_cfg)
    _emit_section(lines, "routing", routing_cfg)
    _emit_section(lines, "mcp", mcp_cfg)
    _emit_section(lines, "images", images_cfg)
    for handle, cfg in agents_out:
        _emit_section(lines, f"agents.{handle}", cfg)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return str(output_path), int(server_cfg["port"])


def _check_requirements(providers: list[str], counts: dict[str, int], base_config: dict, *, dry_run: bool):
    if sys.platform != "darwin":
        print("This launcher currently supports macOS only.")
        sys.exit(1)

    if dry_run:
        return

    if shutil.which("tmux") is None:
        print("tmux is required. Install with: brew install tmux")
        sys.exit(1)

    missing = []
    base_agents = base_config.get("agents", {})
    for provider in providers:
        if counts[provider] <= 0:
            continue
        command = str(base_agents.get(provider, {}).get("command", provider))
        if shutil.which(command) is None:
            missing.append(command)

    if missing:
        print("Missing CLI commands:")
        for command in missing:
            print(f"  - {command}")
        sys.exit(1)


def _launch_room(config_path: str, server_port: int, handles_in_order: list[str], dry_run: bool):
    if dry_run:
        print("\nDry run complete. Config generated only (no launch).")
        return

    python_bin = _ensure_venv()
    if _is_port_listening(server_port):
        print(f"Port {server_port} is already in use.")
        print("Stop the existing server first, then run this launcher again.")
        sys.exit(1)

    config_q = _shell_quote(config_path)
    root_q = _shell_quote(str(ROOT))
    py_q = _shell_quote(str(python_bin))

    server_cmd = f"cd {root_q} && AGENTCHATTR_CONFIG={config_q} {py_q} run.py"
    if not _launch_in_terminal(server_cmd):
        print("Failed to launch server in Terminal.")
        sys.exit(1)

    for _ in range(40):
        if _is_port_listening(server_port):
            break
        time.sleep(0.5)
    else:
        print(f"Server did not start on port {server_port}.")
        sys.exit(1)

    for handle in handles_in_order:
        wrapper_cmd = f"cd {root_q} && AGENTCHATTR_CONFIG={config_q} {py_q} wrapper.py {handle}"
        if not _launch_in_terminal(wrapper_cmd):
            print(f"Failed to launch wrapper for @{handle}.")
            sys.exit(1)
        time.sleep(0.25)

    print("\nRoom launched.")


def main():
    parser = argparse.ArgumentParser(description="Interactive macOS launcher for custom agentchattr rooms")
    parser.add_argument("--dry-run", action="store_true", help="Generate config and show plan without launching")
    parser.add_argument(
        "--config-out",
        default=str(DEFAULT_CONFIG_OUT),
        help="Path to write generated config (default: data/launcher.room.toml)",
    )
    args = parser.parse_args()

    try:
        base_config, base_config_path = _load_base_config()
    except FileNotFoundError as exc:
        print(f"Config not found: {exc}")
        sys.exit(1)

    providers = _cli_providers(base_config)
    if not providers:
        print("No CLI agents found in the active config.")
        sys.exit(1)

    print("agentchattr room launcher (macOS)")
    print(f"Base config: {base_config_path}")
    print("Choose how many instances per provider. Then set each @mention handle.\n")

    counts = {provider: _ask_int(f"How many {provider} instances?", 0) for provider in providers}
    if sum(counts.values()) <= 0:
        print("No agents requested. Nothing to launch.")
        return

    _check_requirements(providers, counts, base_config, dry_run=args.dry_run)

    project_cwd = _ask_project_dir(_default_project_dir(base_config, providers))
    used_handles: set[str] = set()
    handles_by_provider = {provider: [] for provider in providers}
    for provider in providers:
        count = counts[provider]
        for idx in range(1, count + 1):
            default = f"{provider}-{idx}" if count > 1 else provider
            handle = _ask_handle(provider, idx, default, used_handles)
            used_handles.add(handle)
            handles_by_provider[provider].append(handle)

    config_out = Path(args.config_out).expanduser()
    if not config_out.is_absolute():
        config_out = (ROOT / config_out).resolve()

    config_path, server_port = _build_config(
        base_config,
        providers,
        handles_by_provider,
        config_out,
        project_cwd,
    )

    ordered_handles = []
    for provider in providers:
        ordered_handles.extend(handles_by_provider[provider])

    print("\nPlan:")
    print(f"  Config: {config_path}")
    print(f"  Server: http://127.0.0.1:{server_port}")
    print(f"  Project folder: {project_cwd}")
    for provider in providers:
        names = handles_by_provider[provider]
        if names:
            print(f"  {provider}: {' '.join(f'@{name}' for name in names)}")

    confirm = input("\nLaunch now? [Y/n]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("Cancelled. Config file kept for manual launch.")
        return

    _launch_room(config_path, server_port, ordered_handles, args.dry_run)
    if not args.dry_run:
        print(f"Open: http://127.0.0.1:{server_port}")
        print("Use @all or the handles you chose above.")


if __name__ == "__main__":
    main()
