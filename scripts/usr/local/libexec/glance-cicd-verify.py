#!/usr/bin/env python3
"""
Purpose:
    Verify Glance blue-green container health, NPM routing,
    Nginx configuration and direct upstream HTTP access.

Input:
    --expected blue|green

Output:
    JSON verification report and an exit status indicating success.

Safety:
    Strictly read-only. Never creates containers, modifies NPM,
    changes Docker configuration or manipulates deployment state.
"""

import argparse
import json
import re
import subprocess
import sys

NPM = "nginx-proxy-manager"
PORT = 8080

BLUE = "glance"
GREEN = "glance-green-cicd"

BLUE_CONTAINERS = ("glance", "glance-router")
GREEN_CONTAINERS = (
    "glance-green-cicd",
    "glance-router-green-cicd",
)

PROXY_HOSTS = {
    174: BLUE,
    191: BLUE,
    195: BLUE,
}

CANARY_HOST = 198
CANARY_DOMAIN = "glance.blinkwired.com"

HTTP_CHECK = r"""
(async () => {
    const chunks = [];
    for await (const part of process.stdin) chunks.push(part);
    const request = JSON.parse(Buffer.concat(chunks).toString());

    const response = await fetch(request.url, {
        redirect: "manual",
        signal: AbortSignal.timeout(8000)
    });

    console.log(JSON.stringify({
        status: response.status
    }));
})().catch(() => {
    console.error("Upstream HTTP verification failed");
    process.exit(2);
});
"""


def docker(*args, input_data=None):
    """Permit only known read-only Docker operations."""
    allowed = (
        args[:1] == ("inspect",)
        or (
            len(args) == 4
            and args[:3] == ("exec", NPM, "cat")
            and re.fullmatch(
                r"/data/nginx/proxy_host/\d+\.conf",
                args[3],
            )
        )
        or args == ("exec", NPM, "nginx", "-t")
        or (
            len(args) == 6
            and args[:5] == (
                "exec", "-i", NPM, "node", "-e"
            )
            and args[5] == HTTP_CHECK
        )
    )

    if not allowed:
        raise RuntimeError("Disallowed Docker operation")

    result = subprocess.run(
        ["docker", *args],
        input=input_data,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Read-only Docker verification failed: {args[0]}"
        )

    return result.stdout


def check_health(name):
    status = docker(
        "inspect", "-f",
        "{{.State.Health.Status}}",
        name,
    ).strip()

    if status != "healthy":
        raise RuntimeError(
            f"Container {name} is not healthy: {status}"
        )


def proxy_config(host_id):
    return docker(
        "exec", NPM, "cat",
        f"/data/nginx/proxy_host/{host_id}.conf",
    )


def extract_config(config):
    server = re.search(
        r'^\s*set\s+\$server\s+"([^"]+)";',
        config,
        re.MULTILINE,
    )

    port = re.search(
        r'^\s*set\s+\$port\s+"?(\d+)"?;',
        config,
        re.MULTILINE,
    )

    if not server or not port:
        raise RuntimeError(
            "Incomplete generated NPM upstream configuration"
        )

    domains = []

    for match in re.finditer(
        r'^\s*server_name\s+([^;]+);',
        config,
        re.MULTILINE,
    ):
        domains.extend(match.group(1).split())

    return server.group(1), int(port.group(1)), domains


def check_route(host_id, expected, required_domain=None):
    upstream, port, domains = extract_config(
        proxy_config(host_id)
    )

    if upstream != expected:
        raise RuntimeError(
            f"NPM host {host_id}: expected {expected}, "
            f"found {upstream}"
        )

    if port != PORT:
        raise RuntimeError(
            f"NPM host {host_id}: unexpected port {port}"
        )

    if required_domain and required_domain not in domains:
        raise RuntimeError(
            f"NPM host {host_id}: required domain missing"
        )

    return {
        "host_id": host_id,
        "upstream": upstream,
        "port": port,
    }


def check_http(upstream):
    if upstream not in (BLUE, GREEN):
        raise RuntimeError("Unrecognized upstream")

    request = json.dumps({
        "url": f"http://{upstream}:{PORT}/",
    })

    result = docker(
        "exec", "-i", NPM,
        "node", "-e", HTTP_CHECK,
        input_data=request,
    )

    response = json.loads(result)

    if response.get("status") != 200:
        raise RuntimeError(
            f"Upstream {upstream}: HTTP "
            f"{response.get('status')}"
        )


def verify(expected):
    if expected not in ("blue", "green"):
        raise ValueError("Expected mode must be blue or green")

    upstream = BLUE if expected == "blue" else GREEN

    checked = []

    # Blue must remain available as the recovery target.
    for name in BLUE_CONTAINERS:
        check_health(name)
        checked.append(name)

    if expected == "green":
        for name in GREEN_CONTAINERS:
            check_health(name)
            checked.append(name)

    routes = []

    # Other production routes must remain on Blue.
    for host_id, target in PROXY_HOSTS.items():
        routes.append(check_route(host_id, target))

    routes.append(
        check_route(
            CANARY_HOST,
            upstream,
            required_domain=CANARY_DOMAIN,
        )
    )

    docker("exec", NPM, "nginx", "-t")
    check_http(upstream)

    return {
        "status": "verified",
        "mode": expected,
        "upstream": upstream,
        "containers": checked,
        "routes": routes,
        "nginx_syntax": "valid",
        "upstream_http": 200,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected",
        required=True,
        choices=("blue", "green"),
    )

    args = parser.parse_args()

    result = verify(args.expected)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"GLANCE VERIFICATION FAILED: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
