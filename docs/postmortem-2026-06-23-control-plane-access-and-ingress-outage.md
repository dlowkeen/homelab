# Postmortem: Control Plane Access and Ingress Outage (2026-06-23)

## Summary

After several homelab nodes were powered off and then brought back online, the control-plane node became unreachable over SSH even though it was pingable and `sshd` was running. Public app URLs also timed out. Recovery required restoring host firewall behavior, re-establishing ingress edge flow, and fixing an app DNS dependency failure (`immich-server` crash loop on database hostname resolution).

## Impact

- SSH access to control-plane (`192.168.1.246`) failed.
- Public endpoints timed out:
  - `immich.donovanlowkeen.com`
  - `karakeep.donovanlowkeen.com`
- After ingress path recovery, Immich still returned `503` due to backend crash loop.

## Timeline (Condensed)

1. Control-plane responded to ICMP but TCP/22 timed out.
2. Confirmed `sshd` process/service active, but remote SSH unreachable.
3. Determined host packet filtering/rule state issue; emergency flush restored SSH.
4. Public domains still timed out despite DNS resolving to WAN IP.
5. Ingress service/pod existed and had endpoints, but edge traffic path to serving node needed temporary forwarding.
6. Applied temporary DNAT/SNAT workaround using correct netfilter backend (`iptables-legacy`).
7. Domains reached ingress; Immich returned `503`.
8. `immich-server` logs showed `EAI_AGAIN database` (DNS lookup failure), then recovered after DNS path restart/recovery.

## Root Cause

This incident was a stacked failure with three layers:

1. **Host access layer:** Control-plane firewall/netfilter state blocked SSH despite running `sshd`.
2. **Edge ingress layer:** External 80/443 traffic did not cleanly traverse to active ingress serving path until temporary forwarding was applied.
3. **Application dependency layer:** Immich backend crashed due to transient in-cluster DNS resolution failure (`database` hostname lookup).

## Contributing Factors

- Mixed firewall tooling/state (`ufw` inactive while other netfilter state still affected traffic).
- Netfilter backend mismatch risk (`iptables` frontend vs `iptables-legacy` backend) during emergency rules.
- Recovery after power events exposed ordering and state dependencies across host networking, ingress, and DNS.
- Router forwarding constraints (fixed internal ports) complicated direct NodePort recovery path.

## What Worked Well

- Fast decomposition by layer (L3/L4 reachability, ingress routing, backend health).
- Packet captures (`tcpdump`) quickly validated where traffic was arriving.
- Endpoint/pod checks isolated app-level vs edge-level problems.

## What Went Wrong

- Initial symptoms looked singular but were multi-causal.
- Emergency rule application initially targeted the wrong iptables backend, giving zero-hit counters and false confidence.
- No single authoritative firewall baseline had been enforced across all nodes before incident.

## Recovery Actions Taken

- Restored SSH reachability on control-plane by clearing blocking netfilter state.
- Added temporary forwarding from control-plane public 80/443 to ingress-serving worker NodePorts.
- Validated ingress pod/service endpoints and NodePort behavior.
- Recovered Immich backend by resolving DNS path failure and restarting impacted workloads.

## Preventive Actions

1. **Firewall ownership**
   - Enforce one host firewall manager via Ansible.
   - Explicitly disable/mask conflicting firewall services when desired.

2. **Ingress edge design**
   - Use a stable edge pattern that does not depend on ad-hoc host NAT.
   - Prefer direct `80/443` service exposure strategy that matches router capabilities.

3. **Post-reboot smoke checks**
   - SSH to control-plane.
   - Ingress endpoint response checks.
   - CoreDNS health and critical app endpoint validation.

4. **Operational runbook**
   - Add a "node power restoration" checklist with ordered validation steps:
     host reachability -> kube-system health -> ingress -> app dependencies.

## Follow-up Checklist

- [ ] Remove temporary emergency DNAT/SNAT rules after stable ingress path is finalized.
- [ ] Roll out Ansible firewall baseline to all nodes.
- [ ] Add post-reboot smoke test script and document expected outputs.
- [ ] Add alerting for critical CrashLoopBackOff and CoreDNS degradation.
