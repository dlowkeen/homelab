Check all network/firewall related services:
`systemctl list-unit-files --type=service | grep -E 'ufw|nft|netfilter|iptables|network'`

