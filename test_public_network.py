"""

Public Network Access
=====================

Our servers can communicate with the outside world through IPv4 and IPv6.

"""

import pytest
import secrets

from constants import PUBLIC_PING_TARGETS
from ipaddress import ip_interface
from util import nameservers
from util import oneliner
from util import retry_for
from util import reverse_ptr
from util import is_public


@pytest.mark.parametrize('ip_version', [4, 6], ids=['IPv4', 'IPv6'])
def test_public_ip_address_on_all_images(create_server, ip_version, image):
    """ Test that the server has exactly 1 interface with a public IP address
    after at most 30s. Warn if it takes longer than 5s.

    """

    # We want to ensure this works with all images
    server = create_server(image=image)

    # We walk all interfaces to ensure one global address per IP version
    def assert_one_global_address():

        # Get all configured global IP addresses of the given version
        addrs = server.configured_ip_addresses()
        addrs = [a for a in addrs if is_public(a) and a.version == ip_version]

        # Assert a single match
        assert len(addrs) == 1

    # If this takes more than 5 seconds, we show a warning
    retry_for(seconds=5).or_warn(assert_one_global_address, msg=(
        f'{server.name}: No IP address after 5s'))

    # if this all together takes more than 30 seconds, we count it as a failure
    retry_for(seconds=25).or_fail(assert_one_global_address, msg=(
        f'{server.name}: No IP address after 30s'))


def test_public_network_connectivity_on_all_images(server):
    """ Test that the server can ping a set of public ping targets that are
    likely to be online.

    """

    # Ping the targets once the server has been created
    for address in PUBLIC_PING_TARGETS.values():
        server.ping(address, count=3, interval=0.5)

    # Stop/start server to ensure that it works even after that
    server.stop()
    server.start()

    # Ping the targets again, after the server has come back up
    for address in PUBLIC_PING_TARGETS.values():
        server.ping(address, count=3, interval=0.5)


def test_public_network_mtu(server):
    """ Verify that the public interface MTU is exactly 1500 bytes and that
    MTU-sized packages can be exchanged and not exceeded.

    """

    # Get the public interface name
    names = server.output_of('ls /sys/class/net').split()
    iface = next(i for i in names if i != 'lo')

    # Ensure that it is configured with an MTU of 1500 bytes
    mtu = server.output_of(f'cat /sys/class/net/{iface}/mtu')
    assert int(mtu) == 1500

    # Get the address of the DHCP server in use
    #
    # Debian 12 uses systemd-networkd. Therefor, the log entries
    # look way different than, for example Debian 10, with dhclient.
    # Debian 12: DHCPv4, Debian 10: DHCPACK
    ping_target = server.output_of(
        "sudo journalctl | awk '/DHCPv4|DHCPACK/{ip=$NF} END { print ip }'"
    )

    # Try to send a packet using exactly 1500 bytes, which should work. We use
    # a size of 1472, as 8 bytes are used for the ICMP header and another
    # 20 bytes are used for the IP header
    server.ping(ping_target, size=1472, fragment=False)

    # Try to send a packet using 1501 bytes (this should fail)
    server.ping(ping_target, size=1473, fragment=False, expect_failure=True)


@pytest.mark.parametrize('ip_version', [4, 6], ids=['IPv4', 'IPv6'])
def test_public_network_port_security(ip_version, two_servers_in_same_subnet):
    """ Virtual machines should not be able to intercept each others traffic
    through ARP spoofing or NDP poisoning attacks.

    Attention: Due to it's nature this test can cause havoc on the network in
    the absence of effective port security to prevent ARP and ND spoofing!

    This test can reliably detect port security issues in IPv4, but it is
    unreliable when it comes to IPv6, where it can produce false positives.
    """

    # We need two servers in the same subnet
    victim, attacker = two_servers_in_same_subnet

    # For this test, some extra packages are required
    victim.assert_run('sudo apt update --allow-releaseinfo-change')
    victim.assert_run('sudo apt install -y curl')

    attacker.assert_run('sudo apt update --allow-releaseinfo-change')
    attacker.assert_run('sudo apt install -y curl ettercap-text-only tcpdump')

    ipv4 = victim.ip('public', 4)
    ipv6 = victim.ip('public', 6)

    # Get the first IPv6 link local address
    ll_addr_v6 = ip_interface(victim.output_of(
        "ip -6 -brief addr show scope link | awk '{ print $3 }' | head -1"
    )).ip
    ipv4_gateway = victim.gateway('public', 4)
    ipv6_gateway = victim.gateway('public', 6)

    # Use ARP spoofing for IPv4, NDP spoofing for IPv6
    if ip_version == 4:
        victim_ip = ipv4
        curl_params = '-4'
        mtm_method = 'arp'
    else:
        victim_ip = ipv6
        curl_params = '-6'
        mtm_method = 'ndp'

    # This needs to match on IPv4 and IPv6 otherwise // means all addresses
    # and will try to intercept traffic between all hosts on the network
    # Additionally this has to match all addresses used: the IPv6 link-local
    # address and all IPv4 gateway addresses
    ettercap_params = oneliner(f'''
        -M {mtm_method}:remote
        '/{ipv4}/{ipv6};{ll_addr_v6}/'
        '/{ipv4_gateway};{ipv4_gateway+1};{ipv4_gateway+2}/{ipv6_gateway}/'
    ''')

    # The attacker starts poisoning the environment
    attacker.assert_run(oneliner(f"""
        sudo systemd-run --same-dir --unit ettercap
        ettercap -T -w pcap {ettercap_params}
    """))

    # The victim initiates a slow download
    region = victim.zone['slug'][:-1]
    file = f"https://{region}-fixtures.objects.{region}.cloudscale.ch/10mib"

    # Download with 1MiB per second, abort if it takes longer than 15 seconds
    # Don't fail if curl exits with a timeout (exit code 28) or if it fails
    # to connect (exit code 7)
    victim.assert_run(
        f'curl {file} {curl_params} --limit-rate 1M --max-time 15 -O',
        valid_exit_codes=(0, 7, 28),
    )

    # Stop poisoning the network and restore original ARP/ND entries
    attacker.assert_run('sudo systemctl stop ettercap')

    # No TCP packets from or to the victim on port 443 should have been
    # intercepted
    intercepted_packets = int(attacker.output_of(oneliner(f"""
        sudo tcpdump -n -r pcap
        "tcp port 443 and host {victim_ip}"
        | wc -l
    """)))

    assert intercepted_packets == 0


def test_public_network_ipv4_only_on_all_images(prober, create_server, image):
    """ In our tests, IPv6 is enabled by default. To make sure that this is not
    a requirement, we start a server without IPv6 support.

    """

    # Create the server without IPv6 support
    server = create_server(image=image, use_ipv6=False)

    # Make sure the IPv4 address is reachable
    prober.ping(server.ip('public', 4))

    # Count the public addresses per IP version
    v4 = server.configured_ip_addresses(is_public=True, version=4)
    v6 = server.configured_ip_addresses(is_public=True, version=6)

    # Verify there's exactly one IPv4 and no IPv6 address
    assert len(v4) == 1
    assert len(v6) == 0


def test_reverse_ptr_record_of_server(create_server):
    """ The reverse PTR record of a server is set to the name of the server,
    *if* the server name is a FQDN.

    """

    # Create the server with an FQDN
    secret = secrets.token_hex(4)
    server = create_server(name=f'at-{secret}.example.org', auto_name=False)

    # Wait for the reverse PTR records to propagate
    ipv4 = server.ip('public', 4)
    ipv6 = server.ip('public', 6)

    def assert_ptr_propagated():
        for nameserver in nameservers('cloudscale.ch'):
            assert reverse_ptr(ipv4, nameserver) == f'{server.name}.'
            assert reverse_ptr(ipv6, nameserver) == f'{server.name}.'

    retry_for(seconds=60).or_fail(
        assert_ptr_propagated,
        msg=f'Unexpected reverse PTR {server.name} after 60 seconds')


@pytest.mark.parametrize('ip_version', [4, 6], ids=['IPv4', 'IPv6'])
def test_reverse_ptr_record_of_floating_ip(
        create_floating_ip, region, ip_version):
    """ The reverse PTR record of Floating IPs can be set freely. """

    # Create a Floating IP and wait for propagation
    ptr = f"at-{secrets.token_hex(4)}.example.org"
    fip = create_floating_ip(
        ip_version=ip_version, region=region, reverse_ptr=ptr)

    def assert_ptr_propagated():
        for nameserver in nameservers('cloudscale.ch'):
            assert reverse_ptr(fip, nameserver) == f'{ptr}.'

    retry_for(seconds=60).or_fail(
        assert_ptr_propagated,
        msg=f'Unexpected reverse PTR {ptr} after 60 seconds')

    # Update the Floating IP with a non-FQDN value and wait for propagation
    ptr = f"at-{secrets.token_hex(4)}"
    fip.update(reverse_ptr=ptr)

    retry_for(seconds=60).or_fail(
        assert_ptr_propagated,
        msg=f'Unexpected reverse PTR {ptr} after 60 seconds')
