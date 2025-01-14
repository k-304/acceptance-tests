"""

Nested Virtualization Acceptance Tests
====================

Customers can start virtual servers within Plus and Flex servers.

"""


def test_virtualization_support(create_server):
    """ Nested virtualization is supported. """

    # List of virt-host-validate checks that do NOT need to "PASS"
    virt_validate_pass_exceptions = {
        "Checking for device assignment IOMMU support",
        "Checking for secure guest support",
        "Checking for cgroup 'freezer' controller support",
    }

    # Start a server with the flex-4-1 flavor
    server = create_server(flavor='flex-4-1')

    virt_validate_status = server.prepare_nested_virtualization()

    # Validate all checks PASS except for the ones defined above
    for line in virt_validate_status.splitlines():
        parts = line.split(':')
        description = parts[1].strip()
        status = parts[2].strip().split()[0]

        if description not in virt_validate_pass_exceptions:
            assert status == "PASS"


def test_run_nested_vm(create_server):
    """ Nested virtualization is supported. """

    vm_os = 'alpinelinux3.17'   # Needs to match one virt os-variant
    vm_iso_url = (
        'https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/'
        'alpine-virt-3.21.2-x86_64.iso'
    )

    # Start a server with the flex-4-1 flavor
    server = create_server(flavor='flex-4-1')

    vm_status = server.setup_nested_virtualization(vm_os, vm_iso_url)

    assert vm_os in vm_status and 'running' in vm_status
