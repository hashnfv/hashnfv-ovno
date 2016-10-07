#!/usr/bin/env python

from subprocess import CalledProcessError
import sys

from apt_pkg import version_compare
import json
import uuid
import yaml

from charmhelpers.core.hookenv import (
    Hooks,
    UnregisteredHookError,
    config,
    is_leader,
    leader_get,
    leader_set,
    log,
    relation_get,
    relation_ids,
    relation_set
)

from charmhelpers.core.host import (
    restart_on_change,
    service_restart
)

from charmhelpers.fetch import (
    apt_install,
    apt_upgrade,
    configure_sources
)

from neutron_contrail_utils import (
    CONTRAIL_VERSION,
    OPENSTACK_VERSION,
    configure_vrouter,
    disable_vrouter_vgw,
    dpkg_version,
    drop_caches,
    enable_vrouter_vgw,
    fix_nodemgr,
    fix_permissions,
    fix_vrouter_scripts,
    ifdown,
    ifup,
    modprobe,
    provision_local_metadata,
    provision_vrouter,
    remove_juju_bridge,
    units,
    unprovision_local_metadata,
    unprovision_vrouter,
    write_barbican_auth_config,
    write_nodemgr_config,
    write_vnc_api_config,
    write_vrouter_config,
    write_vrouter_vgw_interfaces
)

PACKAGES = [ "contrail-vrouter-dkms", "contrail-vrouter-agent",
             "contrail-nova-driver", "contrail-utils", "python-jinja2",
             "python-netifaces", "python-netaddr", "contrail-nodemgr" ]

PACKAGES_LBAAS = [ "python-barbicanclient", "haproxy" ]

hooks = Hooks()
config = config()

def check_local_metadata():
    if not is_leader():
        return

    if not config.get("vrouter-provisioned"):
        if leader_get("local-metadata-provisioned"):
            # impossible to know if current hook is firing because
            # relation or leader is being removed lp #1469731
            if not relation_ids("cluster"):
                unprovision_local_metadata()
            leader_set({"local-metadata-provisioned": ""})
        return

    if config["local-metadata-server"]:
        if not leader_get("local-metadata-provisioned"):
            provision_local_metadata()
            leader_set({"local-metadata-provisioned": True})
    elif leader_get("local-metadata-provisioned"):
        unprovision_local_metadata()
        leader_set({"local-metadata-provisioned": ""})

def check_vrouter():
    # check relation dependencies
    if config_get("contrail-api-ready") \
       and config_get("control-node-ready") \
       and config_get("identity-admin-ready"):
        if not config_get("vrouter-provisioned"):
            provision_vrouter()
            config["vrouter-provisioned"] = True
    elif config_get("vrouter-provisioned"):
        unprovision_vrouter()
        config["vrouter-provisioned"] = False

@hooks.hook("config-changed")
def config_changed():
    configure_local_metadata()
    configure_virtual_gateways()
    write_config()
    if not units("contrail-discovery") and not units("control-node"):
        config["control-node-ready"] = True if config.get("discovery-server-ip") \
                                            else False
    if not units("contrail-api"):
        config["contrail-api-ready"] = True if config.get("contrail-api-ip") \
                                            else False
    check_vrouter()
    check_local_metadata()

def config_get(key):
    try:
        return config[key]
    except KeyError:
        return None

def configure_local_metadata():
    if config["local-metadata-server"]:
        if "local-metadata-secret" not in config:
            # generate secret
            secret = str(uuid.uuid4())
            config["local-metadata-secret"] = secret
            settings = { "metadata-shared-secret": secret }
            # inform relations
            for rid in relation_ids("neutron-plugin"):
                relation_set(relation_id=rid, relation_settings=settings)
    else:
        if "local-metadata-secret" in config:
            # remove secret
            del config["local-metadata-secret"]
            settings = { "metadata-shared-secret": None }
            # inform relations
            for rid in relation_ids("neutron-plugin"):
                relation_set(relation_id=rid, relation_settings=settings)

def configure_virtual_gateways():
    gateways = config.get("virtual-gateways")
    previous_gateways = config_get("virtual-gateways-prev")
    if gateways != previous_gateways:
        # create/destroy virtual gateway interfaces according to new value
        interfaces = { gateway["interface"]: set(gateway["subnets"])
                       for gateway in yaml.safe_load(gateways) } \
                     if gateways else {}
        previous_interfaces = { gateway["interface"]: set(gateway["subnets"])
                                for gateway in yaml.safe_load(previous_gateways) } \
                              if previous_gateways else {}
        ifaces = [ interface for interface, subnets in previous_interfaces.iteritems()
                   if interface not in interfaces
                   or subnets != interfaces[interface] ]
        if ifaces:
            ifdown(ifaces)

        write_vrouter_vgw_interfaces()

        ifaces = [ interface for interface, subnets in interfaces.iteritems()
                   if interface not in previous_interfaces
                   or subnets != previous_interfaces[interface] ]
        if ifaces:
            ifup(ifaces)

        if interfaces:
            enable_vrouter_vgw()
        else:
            disable_vrouter_vgw()

        config["virtual-gateways-prev"] = gateways

@hooks.hook("contrail-api-relation-departed")
@hooks.hook("contrail-api-relation-broken")
def contrail_api_departed():
    if not units("contrail-api") and not config.get("contrail-api-ip"):
        config["contrail-api-ready"] = False
        check_vrouter()
        check_local_metadata()
    write_vnc_api_config()

@hooks.hook("contrail-api-relation-changed")
def contrail_api_changed():
    if not relation_get("port"):
        log("Relation not ready")
        return
    write_vnc_api_config()
    config["contrail-api-ready"] = True
    check_vrouter()
    check_local_metadata()

@hooks.hook("contrail-discovery-relation-changed")
def contrail_discovery_changed():
    if not relation_get("port"):
        log("Relation not ready")
        return
    contrail_discovery_relation()
    config["control-node-ready"] = True
    check_vrouter()
    check_local_metadata()

@hooks.hook("contrail-discovery-relation-departed")
@hooks.hook("contrail-discovery-relation-broken")
def contrail_discovery_departed():
    if not units("contrail-discovery") \
       and not units("control-node") \
       and not config.get("discovery-server-ip"):
        config["control-node-ready"] = False
        check_vrouter()
        check_local_metadata()
    contrail_discovery_relation()

@restart_on_change({"/etc/contrail/contrail-vrouter-agent.conf": ["contrail-vrouter-agent"],
                    "/etc/contrail/contrail-vrouter-nodemgr.conf": ["contrail-vrouter-nodemgr"]})
def contrail_discovery_relation():
    write_vrouter_config()
    write_nodemgr_config()

@hooks.hook("control-node-relation-departed")
@hooks.hook("control-node-relation-broken")
def control_node_departed():
    if not units("control-node") \
       and not units("contrail-discovery") \
       and not config.get("discovery-server-ip"):
        config["control-node-ready"] = False
        check_vrouter()
        check_local_metadata()
    control_node_relation()

@hooks.hook("control-node-relation-joined")
def control_node_joined():
    control_node_relation()
    config["control-node-ready"] = True
    check_vrouter()
    check_local_metadata()

@restart_on_change({"/etc/contrail/contrail-vrouter-agent.conf": ["contrail-vrouter-agent"]})
def control_node_relation():
    write_vrouter_config()

@hooks.hook("identity-admin-relation-changed")
def identity_admin_changed():
    if not relation_get("service_hostname"):
        log("Relation not ready")
        return
    write_vnc_api_config()
    if version_compare(CONTRAIL_VERSION, "3.0.2.0-34") >= 0:
        write_barbican_auth_config()
    config["identity-admin-ready"] = True
    check_vrouter()
    check_local_metadata()

@hooks.hook("identity-admin-relation-departed")
@hooks.hook("identity-admin-relation-broken")
def identity_admin_departed():
    if not units("identity-admin"):
        config["identity-admin-ready"] = False
        check_vrouter()
        check_local_metadata()
    write_vnc_api_config()
    if version_compare(CONTRAIL_VERSION, "3.0.2.0-34") >= 0:
        write_barbican_auth_config()

@hooks.hook()
def install():
    configure_sources(True, "install-sources", "install-keys")
    apt_upgrade(fatal=True, dist=True)
    fix_vrouter_scripts() # bug in 2.0+20141015.1 packages
    apt_install(PACKAGES, fatal=True)

    contrail_version = dpkg_version("contrail-vrouter-agent")
    openstack_version = dpkg_version("nova-compute")
    if version_compare(contrail_version, "3.0.2.0-34") >= 0 \
       and version_compare(openstack_version, "2:12.0.0") >= 0:
        # install lbaas packages
        apt_install(PACKAGES_LBAAS, fatal=True)

    fix_permissions()
    fix_nodemgr()
    if config.get("remove-juju-bridge"):
        remove_juju_bridge()
    try:
        modprobe("vrouter")
    except CalledProcessError:
        log("vrouter kernel module failed to load, clearing pagecache and retrying")
        drop_caches()
        modprobe("vrouter")
    modprobe("vrouter", True, True)
    configure_vrouter()
    service_restart("nova-compute")

@hooks.hook("neutron-metadata-relation-changed")
def neutron_metadata_changed():
    if not relation_get("shared-secret"):
        log("Relation not ready")
        return
    neutron_metadata_relation()

@hooks.hook("neutron-metadata-relation-departed")
@hooks.hook("neutron-metadata-relation-broken")
@restart_on_change({"/etc/contrail/contrail-vrouter-agent.conf": ["contrail-vrouter-agent"]})
def neutron_metadata_relation():
    write_vrouter_config()

@hooks.hook("neutron-plugin-relation-joined")
def neutron_plugin_joined():
    # create plugin config
    section = []
    if version_compare(OPENSTACK_VERSION, "1:2015.1~") < 0:
        if version_compare(OPENSTACK_VERSION, "1:2014.2") >= 0:
            section.append(("network_api_class", "nova_contrail_vif.contrailvif.ContrailNetworkAPI"))
        else:
            section.append(("libvirt_vif_driver", "nova_contrail_vif.contrailvif.VRouterVIFDriver"))
    section.append(("firewall_driver", "nova.virt.firewall.NoopFirewallDriver"))
    conf = {
      "nova-compute": {
        "/etc/nova/nova.conf": {
          "sections": {
            "DEFAULT": section
          }
        }
      }
    }
    relation_set(subordinate_configuration=json.dumps(conf))

    if config["local-metadata-server"]:
        settings = { "metadata-shared-secret": config["local-metadata-secret"] }
        relation_set(relation_settings=settings)

def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log("Unknown hook {} - skipping.".format(e))

@hooks.hook("upgrade-charm")
def upgrade_charm():
    write_vrouter_config()
    write_vnc_api_config()
    write_nodemgr_config()
    service_restart("supervisor-vrouter")

@restart_on_change({"/etc/contrail/contrail-vrouter-agent.conf": ["contrail-vrouter-agent"],
                    "/etc/contrail/contrail-vrouter-nodemgr.conf": ["contrail-vrouter-nodemgr"]})
def write_config():
    write_vrouter_config()
    write_vnc_api_config()
    write_nodemgr_config()

if __name__ == "__main__":
    main()
