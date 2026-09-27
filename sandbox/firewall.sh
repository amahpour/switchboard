#!/bin/bash
# Egress firewall for the switchboard box (docs/SANDBOX.md §5). Runs as root from the
# entrypoint, inside the container's own network namespace only; any failure
# stops the container (fail closed). Agents get: DNS through dnsmasq for
# allowlisted domains only, TCP 443 to the IPs those lookups returned, loopback,
# and inbound connections to the published switchboard port. No IPv6.
set -euo pipefail
# Root-owned directories only: never run a binary a dev process could plant.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}' > /run/switchboard-host-ip || true   # for the checks
ipset create allowed hash:ip -exist
# group= (empty) skips setgroups(), which needs CAP_SETGID; user=root skips setuid().
{ printf '%s\n' user=root group= listen-address=127.0.0.1 bind-interfaces \
    no-resolv no-hosts filter-AAAA log-queries log-facility=/var/log/switchboard-dns.log
  for d in $(sed 's/#.*//' /opt/sandbox/allowlist.txt); do
    echo "server=/$d/127.0.0.11"; echo "ipset=/$d/allowed"; done
} > /etc/dnsmasq-switchboard.conf
dnsmasq --conf-file=/etc/dnsmasq-switchboard.conf
echo 'nameserver 127.0.0.1' > /etc/resolv.conf
iptables -F; iptables -X                       # filter table only; Docker's DNS NAT rules stay
iptables -P INPUT DROP; iptables -P FORWARD DROP; iptables -P OUTPUT DROP
iptables -A OUTPUT -d 127.0.0.11 -m owner ! --uid-owner 0 -j REJECT   # agents can't bypass dnsmasq
iptables -A INPUT -i lo -j ACCEPT; iptables -A OUTPUT -o lo -j ACCEPT
for c in INPUT OUTPUT; do iptables -A "$c" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; done
for p in udp tcp; do iptables -A OUTPUT -p "$p" --dport 53 -m owner --uid-owner 0 -j ACCEPT; done  # dockerd's upstream DNS
iptables -A INPUT -p tcp --dport "${SWITCHBOARD_PORT:-8765}" -m conntrack --ctstate NEW -j ACCEPT
iptables -A OUTPUT -p tcp --dport 443 -m set --match-set allowed dst -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited
for c in INPUT FORWARD OUTPUT; do ip6tables -P "$c" DROP; done
ip6tables -A INPUT -i lo -j ACCEPT; ip6tables -A OUTPUT -o lo -j ACCEPT
# Say "on" only if the policies really are in place.
# (grep without -q reads all input: no SIGPIPE for pipefail to trip on.)
{ iptables -S OUTPUT | grep -x -- '-P OUTPUT DROP' && iptables -S INPUT | grep -x -- '-P INPUT DROP' \
  && ip6tables -S OUTPUT | grep -x -- '-P OUTPUT DROP'; } >/dev/null \
  || { echo "switchboard firewall: policies not applied" >&2; exit 1; }
echo "switchboard firewall: on" >&2
