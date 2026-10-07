#!/usr/bin/env python3
"""Summarize docker inspect JSON without echoing commands, secrets or host IPs.

Usage: docker inspect <container names> | python3 tools/check_container_startup.py
Raw inspect data stays in the pipe; only whitelisted flags are printed.
"""
import json
import sys


def summarize(container):
    config = container.get('Config') or {}
    command = config.get('Cmd') or []
    if isinstance(command, str):
        command = [command]
    targets = [str(value).rsplit('/', 1)[-1] for value in command]
    state = container.get('State') or {}
    status = state.get('Status')
    health = (state.get('Health') or {}).get('Status')
    return {
        'preview_command': 'preview.py' in targets,
        'legacy_app_command': 'app.py' in targets,
        'explicit_input': '--input' in command,
        'camera_config_option': '--cameras' in command,
        'env_file_option': '--env-file' in command,
        'diagnostics': '--diagnostics' in command,
        'nvidia_entrypoint': any(str(value).endswith('/nvidia_entrypoint.sh')
                                 for value in config.get('Entrypoint') or []),
        'canonical_working_dir': config.get('WorkingDir') == '/workspace/ReTrace',
        'project_bind_mount': any(mount.get('Type') == 'bind' and
                                  mount.get('Destination') == '/workspace/ReTrace'
                                  for mount in container.get('Mounts') or []),
        'auto_remove': bool((container.get('HostConfig') or {}).get('AutoRemove')),
        'status': status if status in ('created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead') else 'unknown',
        'health': health if health in ('starting', 'healthy', 'unhealthy') else None,
    }


def main():
    try:
        containers = json.load(sys.stdin)
        if not isinstance(containers, list):
            raise ValueError()
        for index, container in enumerate(containers):
            print(json.dumps({'inspect_index': index, **summarize(container)}))
    except (ValueError, TypeError, AttributeError):
        print('Cannot parse inspect data; raw details hidden', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
