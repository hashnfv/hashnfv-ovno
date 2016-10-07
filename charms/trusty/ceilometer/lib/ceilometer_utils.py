import os
import shutil
import uuid

from collections import OrderedDict

import yaml

from charmhelpers.contrib.openstack import (
    templating,
    context,
)
from ceilometer_contexts import (
    ApacheSSLContext,
    LoggingConfigContext,
    MongoDBContext,
    CeilometerContext,
    HAProxyContext,
    CEILOMETER_PORT,
)
from charmhelpers.contrib.openstack.utils import (
    get_os_codename_package,
    get_os_codename_install_source,
    configure_installation_source,
    pause_unit,
    resume_unit,
    make_assess_status_func,
)
from charmhelpers.core.hookenv import (
    config,
    log,
    related_units,
    relation_get,
    relation_ids,
)
from charmhelpers.fetch import apt_update, apt_install, apt_upgrade
from copy import deepcopy

HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
CEILOMETER_CONF_DIR = "/etc/ceilometer"
CEILOMETER_CONF = "%s/ceilometer.conf" % CEILOMETER_CONF_DIR
CEILOMETER_PIPELINE_CONF = '%s/pipeline.yaml' % CEILOMETER_CONF_DIR
CEILOMETER_PIPELINE_CONF_ORIG = '%s.orig' % CEILOMETER_PIPELINE_CONF
HTTPS_APACHE_CONF = "/etc/apache2/sites-available/openstack_https_frontend"
HTTPS_APACHE_24_CONF = "/etc/apache2/sites-available/" \
    "openstack_https_frontend.conf"
CLUSTER_RES = 'grp_ceilometer_vips'

CEILOMETER_BASE_SERVICES = [
    'ceilometer-agent-central',
    'ceilometer-collector',
    'ceilometer-api',
]

ICEHOUSE_SERVICES = [
    'ceilometer-alarm-notifier',
    'ceilometer-alarm-evaluator',
    'ceilometer-agent-notification'
]

MITAKA_SERVICES = [
    'ceilometer-agent-notification'
]

CEILOMETER_DB = "ceilometer"
CEILOMETER_SERVICE = "ceilometer"

CEILOMETER_BASE_PACKAGES = [
    'haproxy',
    'apache2',
    'ceilometer-agent-central',
    'ceilometer-collector',
    'ceilometer-api',
    'python-pymongo',
]

ICEHOUSE_PACKAGES = [
    'ceilometer-alarm-notifier',
    'ceilometer-alarm-evaluator',
    'ceilometer-agent-notification'
]

MITAKA_PACKAGES = [
    'ceilometer-agent-notification'
]

REQUIRED_INTERFACES = {
    'database': ['mongodb'],
    'messaging': ['amqp'],
    'identity': ['identity-service'],
}

CEILOMETER_ROLE = "ResellerAdmin"
SVC = 'ceilometer'

CONFIG_FILES = OrderedDict([
    (CEILOMETER_CONF, {
        'hook_contexts': [context.IdentityServiceContext(service=SVC,
                                                         service_user=SVC),
                          context.AMQPContext(ssl_dir=CEILOMETER_CONF_DIR),
                          LoggingConfigContext(),
                          MongoDBContext(),
                          CeilometerContext(),
                          context.SyslogContext(),
                          HAProxyContext()],
        'services': CEILOMETER_BASE_SERVICES
    }),
    (HAPROXY_CONF, {
        'hook_contexts': [context.HAProxyContext(singlenode_mode=True),
                          HAProxyContext()],
        'services': ['haproxy'],
    }),
    (HTTPS_APACHE_CONF, {
        'hook_contexts': [ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (HTTPS_APACHE_24_CONF, {
        'hook_contexts': [ApacheSSLContext()],
        'services': ['apache2'],
    })
])

TEMPLATES = 'templates'

JUJU_HEADER = '# [ WARNING ] config file maintained by Juju, local changes may be overwritten.'

SHARED_SECRET = "/etc/ceilometer/secret.txt"


def ordereddict_constructor(loader, node):
    return OrderedDict(loader.construct_pairs(node))


def ordereddict_representer(dumper, data):
    return dumper.represent_mapping('tag:yaml.org,2002:map', data.items())


yaml.add_constructor('tag:yaml.org,2002:map', ordereddict_constructor)
yaml.add_representer(OrderedDict, ordereddict_representer)


def register_configs():
    """
    Register config files with their respective contexts.
    Regstration of some configs may not be required depending on
    existing of certain relations.
    """
    # if called without anything installed (eg during install hook)
    # just default to earliest supported release. configs dont get touched
    # till post-install, anyway.
    release = get_os_codename_package('ceilometer-common', fatal=False) \
        or 'grizzly'
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)

    for conf in CONFIG_FILES:
        configs.register(conf, CONFIG_FILES[conf]['hook_contexts'])

    if os.path.exists('/etc/apache2/conf-available'):
        configs.register(HTTPS_APACHE_24_CONF,
                         CONFIG_FILES[HTTPS_APACHE_24_CONF]['hook_contexts'])
    else:
        configs.register(HTTPS_APACHE_CONF,
                         CONFIG_FILES[HTTPS_APACHE_CONF]['hook_contexts'])
    return configs


def restart_map():
    """
    Determine the correct resource map to be passed to
    charmhelpers.core.restart_on_change() based on the services configured.

    :returns: dict: A dictionary mapping config file to lists of services
                    that should be restarted when file changes.
    """
    _map = {}
    for f, ctxt in CONFIG_FILES.iteritems():
        svcs = []
        for svc in ctxt['services']:
            svcs.append(svc)
        if f == CEILOMETER_CONF:
            for svc in ceilometer_release_services():
                svcs.append(svc)
        if svcs:
            _map[f] = svcs

    _map[CEILOMETER_PIPELINE_CONF] = CEILOMETER_BASE_SERVICES

    return _map


def services():
    """ Returns a list of services associate with this charm """
    _services = []
    for v in restart_map().values():
        _services = _services + v
    return list(set(_services))


def determine_ports():
    """Assemble a list of API ports for services the charm is managing

    @returns [ports] - list of ports that the charm manages.
    """
    # TODO(ajkavanagh) - determine what other ports the service listens on
    # apart from the main CEILOMETER port
    ports = [CEILOMETER_PORT]
    return ports


def get_ceilometer_context():
    """ Retrieve a map of all current relation data for agent configuration """
    ctxt = {}
    for hcontext in CONFIG_FILES[CEILOMETER_CONF]['hook_contexts']:
        ctxt.update(hcontext())
    return ctxt


def do_openstack_upgrade(configs):
    """
    Perform an upgrade.  Takes care of upgrading packages, rewriting
    configs, database migrations and potentially any other post-upgrade
    actions.

    :param configs: The charms main OSConfigRenderer object.
    """
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)

    log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_update(fatal=True)
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    apt_install(packages=get_packages(),
                options=dpkg_opts,
                fatal=True)

    # set CONFIGS to load templates from new release
    configs.set_release(openstack_release=new_os_rel)


def ceilometer_release_services():
    codename = get_os_codename_install_source(config('openstack-origin'))
    if codename >= 'mitaka':
        return MITAKA_SERVICES
    elif codename >= 'icehouse':
        return ICEHOUSE_SERVICES
    else:
        return []


def ceilometer_release_packages():
    codename = get_os_codename_install_source(config('openstack-origin'))
    if codename >= 'mitaka':
        return MITAKA_PACKAGES
    elif codename >= 'icehouse':
        return ICEHOUSE_PACKAGES
    else:
        return []


def get_packages():
    packages = (deepcopy(CEILOMETER_BASE_PACKAGES) +
                ceilometer_release_packages())
    return packages


def get_shared_secret():
    """
    Returns the current shared secret for the ceilometer node. If the shared
    secret does not exist, this method will generate one.
    """
    secret = None
    if not os.path.exists(SHARED_SECRET):
        secret = str(uuid.uuid4())
        set_shared_secret(secret)
    else:
        with open(SHARED_SECRET, 'r') as secret_file:
            secret = secret_file.read().strip()
    return secret


def set_shared_secret(secret):
    """
    Sets the shared secret which is used to sign ceilometer messages.

    :param secret: the secret to set
    """
    with open(SHARED_SECRET, 'w') as secret_file:
        secret_file.write(secret)


def assess_status(configs):
    """Assess status of current unit

    Decides what the state of the unit should be based on the current
    configuration.

    SIDE EFFECT: calls set_os_workload_status(...) which sets the workload
    status of the unit.
    Also calls status_set(...) directly if paused state isn't complete.

    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    assess_status_func(configs)()


def assess_status_func(configs):
    """Helper function to create the function that will assess_status() for
    the unit.
    Uses charmhelpers.contrib.openstack.utils.make_assess_status_func() to
    create the appropriate status function and then returns it.
    Used directly by assess_status() and also for pausing and resuming
    the unit.

    @param configs: a templating.OSConfigRenderer() object
    @return f() -> None : a function that assesses the unit's workload status
    """
    return make_assess_status_func(
        configs, REQUIRED_INTERFACES,
        services=services(), ports=determine_ports())


def pause_unit_helper(configs):
    """Helper function to pause a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.pause_unit() to do the work.

    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(pause_unit, configs)


def resume_unit_helper(configs):
    """Helper function to resume a unit, and then call assess_status(...) in
    effect, so that the status is correctly updated.
    Uses charmhelpers.contrib.openstack.utils.resume_unit() to do the work.

    @param configs: a templating.OSConfigRenderer() object
    @returns None - this function is executed for its side-effect
    """
    _pause_resume_helper(resume_unit, configs)


def _pause_resume_helper(f, configs):
    """Helper function that uses the make_assess_status_func(...) from
    charmhelpers.contrib.openstack.utils to create an assess_status(...)
    function that can be used with the pause/resume of the unit

    @param f: the function to be used with the assess_status(...) function
    @returns None - this function is executed for its side-effect
    """
    # TODO(ajkavanagh) - ports= has been left off because of the race hazard
    # that exists due to service_start()
    f(assess_status_func(configs),
      services=services(),
      ports=determine_ports())


def configure_pipeline():
    if not os.path.exists(CEILOMETER_PIPELINE_CONF_ORIG):
        shutil.copy(CEILOMETER_PIPELINE_CONF,
                    CEILOMETER_PIPELINE_CONF_ORIG)
    with open(CEILOMETER_PIPELINE_CONF_ORIG) as f:
        conf = yaml.load(f)

    sources = conf['sources']
    sinks = conf['sinks']
    for rid in relation_ids('ceilometer-plugin'):
        for unit in related_units(rid):
            srcs = relation_get('meter-sources', unit, rid)
            if srcs:
                srcs = yaml.load(srcs)
                sources.extend(srcs)
            sks = relation_get('meter-sinks', unit, rid)
            if sks:
                sks = yaml.load(sks)
                sinks.extend(sks)

    log('Writing config %s' % CEILOMETER_PIPELINE_CONF)
    with open(CEILOMETER_PIPELINE_CONF, 'w') as f:
        f.write(JUJU_HEADER)
        f.write('\n')
        yaml.dump(conf, f, default_flow_style=False, explicit_start=True)
