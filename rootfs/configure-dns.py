"""Render the reviewed IPv4 client list into both image policies."""

import argparse
from ipaddress import IPv4Network
from pathlib import Path


def render(root, clients):
    networks = set()
    if clients.exists():
        for number, line in enumerate(clients.read_text().splitlines(), 1):
            cidr = line.partition("#")[0].strip()
            if not cidr:
                continue
            try:
                network = IPv4Network(cidr)
                if not network.prefixlen or network.overlaps(IPv4Network("127.0.0.0/8")):
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid IPv4 client network at line {number}") from None
            networks.add(network)
    networks = sorted(networks)
    pending = []
    for name, marker, rule in (
        ("etc/i14/firewall.rules", "# I14_DNS_CLIENTS", "-A I14_DNS -s {} -j ACCEPT"),
        ("etc/unbound/unbound.conf", "\t# I14_DNS_CLIENTS", "\taccess-control: {} allow"),
    ):
        path = root / name
        template = path.read_text()
        if template.splitlines().count(marker) != 1:
            raise ValueError(f"Missing or duplicate DNS clients marker in {name}")
        pending.append((path, template.replace(marker + "\n", "".join(rule.format(n) + "\n" for n in networks))))
    for path, config in pending:
        path.write_text(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("clients", type=Path)
    args = parser.parse_args()
    try:
        render(args.root, args.clients)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"DNS policy build failed: {exc}\n")
