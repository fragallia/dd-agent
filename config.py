import ConfigParser
import os
import itertools
import logging
import logging.config
import logging.handlers
import platform
import string
import subprocess
import sys
import glob
import inspect
import traceback
import imp
from optparse import OptionParser, Values
from cStringIO import StringIO

from util import get_os

# CONSTANTS
DATADOG_CONF = "datadog.conf"
DEFAULT_CHECK_FREQUENCY = 15   # seconds
DEFAULT_STATSD_FREQUENCY = 10  # seconds
PUP_STATSD_FREQUENCY = 2       # seconds
LOGGING_MAX_BYTES = 5 * 1024 * 1024

log = logging.getLogger(__name__)


class PathNotFound(Exception):
    pass


def get_parsed_args():
    parser = OptionParser()
    parser.add_option('-d', '--dd_url', action='store', default=None,
                        dest='dd_url')
    parser.add_option('-c', '--clean', action='store_true', default=False,
                        dest='clean')
    parser.add_option('-u', '--use-local-forwarder', action='store_true',
                        default=False, dest='use_forwarder')
    parser.add_option('-n', '--disable-dd', action='store_true', default=False,
                        dest="disable_dd")
    parser.add_option('-v', '--verbose', action='store_true', default=False,
                        dest='verbose',
                      help='Print out stacktraces for errors in checks')

    try:
        options, args = parser.parse_args()
    except SystemExit:
        # Ignore parse errors
        options, args = Values({'dd_url': None,
                                'clean': False,
                                'use_forwarder':False,
                                'disable_dd':False,
                                'use_forwarder': False}), []
    return options, args


def get_version():
    return "3.6.2"


def skip_leading_wsp(f):
    "Works on a file, returns a file-like object"
    return StringIO("\n".join(map(string.strip, f.readlines())))


def _windows_commondata_path():
    """Return the common appdata path, using ctypes
    From http://stackoverflow.com/questions/626796/\
    how-do-i-find-the-windows-common-application-data-folder-using-python
    """
    import ctypes
    from ctypes import wintypes, windll

    CSIDL_COMMON_APPDATA = 35

    _SHGetFolderPath = windll.shell32.SHGetFolderPathW
    _SHGetFolderPath.argtypes = [wintypes.HWND,
                                ctypes.c_int,
                                wintypes.HANDLE,
                                wintypes.DWORD, wintypes.LPCWSTR]

    path_buf = wintypes.create_unicode_buffer(wintypes.MAX_PATH)
    result = _SHGetFolderPath(0, CSIDL_COMMON_APPDATA, 0, 0, path_buf)
    return path_buf.value


def _windows_config_path():
    common_data = _windows_commondata_path()
    path = os.path.join(common_data, 'Datadog', DATADOG_CONF)
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _windows_confd_path():
    common_data = _windows_commondata_path()
    path = os.path.join(common_data, 'Datadog', 'conf.d')
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _windows_checksd_path():
    if hasattr(sys, 'frozen'):
        # we're frozen - from py2exe
        prog_path = os.path.dirname(sys.executable)
        checksd_path = os.path.join(prog_path, 'checks.d')
    else:

        cur_path = os.path.dirname(__file__)
        checksd_path = os.path.join(cur_path, 'checks.d')

    if os.path.exists(checksd_path):
        return checksd_path
    raise PathNotFound(checksd_path)


def _unix_config_path():
    path = os.path.join('/etc/dd-agent', DATADOG_CONF)
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _unix_confd_path():
    path = os.path.join('/etc/dd-agent', 'conf.d')
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _unix_checksd_path():
    # Unix only will look up based on the current directory
    # because checks.d will hang with the other python modules
    cur_path = os.path.dirname(os.path.realpath(__file__))
    checksd_path = os.path.join(cur_path, 'checks.d')

    if os.path.exists(checksd_path):
        return checksd_path
    raise PathNotFound(checksd_path)


def _is_affirmative(s):
    return s.lower() in ('yes', 'true')


def get_config_path(cfg_path=None, os_name=None):
    # Check if there's an override and if it exists
    if cfg_path is not None and os.path.exists(cfg_path):
        return cfg_path

    if os_name is None:
        os_name = get_os()

    # Check for an OS-specific path, continue on not-found exceptions
    bad_path = ''
    if os_name == 'windows':
        try:
            return _windows_config_path()
        except PathNotFound, e:
            if len(e.args) > 0:
                bad_path = e.args[0]
    else:
        try:
            return _unix_config_path()
        except PathNotFound, e:
            if len(e.args) > 0:
                bad_path = e.args[0]

    # Check if there's a config stored in the current agent directory
    path = os.path.realpath(__file__)
    path = os.path.dirname(path)
    if os.path.exists(os.path.join(path, DATADOG_CONF)):
        return os.path.join(path, DATADOG_CONF)

    # If all searches fail, exit the agent with an error
    sys.stderr.write("Please supply a configuration file at %s or in the directory where the agent is currently deployed.\n" % bad_path)
    sys.exit(3)


def get_config(parse_args=True, cfg_path=None, options=None):
    if parse_args:
        options, args = get_parsed_args()

    # General config
    agentConfig = {
        'check_freq': DEFAULT_CHECK_FREQUENCY,
        'dogstatsd_interval': DEFAULT_STATSD_FREQUENCY,
        'dogstatsd_normalize': 'yes',
        'dogstatsd_port': 8125,
        'dogstatsd_target': 'http://localhost:17123',
        'graphite_listen_port': None,
        'hostname': None,
        'listen_port': None,
        'tags': None,
        'use_ec2_instance_id': False,  # DEPRECATED
        'version': get_version(),
        'watchdog': True,
        'additional_checksd': '/etc/dd-agent/checks.d/',
    }

    dogstatsd_interval = DEFAULT_STATSD_FREQUENCY

    # Config handling
    try:
        # Find the right config file
        path = os.path.realpath(__file__)
        path = os.path.dirname(path)

        config_path = get_config_path(cfg_path, os_name=get_os())
        config = ConfigParser.ConfigParser()
        config.readfp(skip_leading_wsp(open(config_path)))

        # bulk import
        for option in config.options('Main'):
            agentConfig[option] = config.get('Main', option)

        #
        # Core config
        #

        # FIXME unnecessarily complex

        if config.has_option('Main', 'use_dd'):
            agentConfig['use_dd'] = config.get('Main', 'use_dd').lower() in ("yes", "true")
        else:
            agentConfig['use_dd'] = True

        agentConfig['use_forwarder'] = False
        if options is not None and options.use_forwarder:
            listen_port = 17123
            if config.has_option('Main', 'listen_port'):
                listen_port = int(config.get('Main', 'listen_port'))
            agentConfig['dd_url'] = "http://localhost:" + str(listen_port)
            agentConfig['use_forwarder'] = True
        elif options is not None and not options.disable_dd and options.dd_url:
            agentConfig['dd_url'] = options.dd_url
        else:
            agentConfig['dd_url'] = config.get('Main', 'dd_url')
        if agentConfig['dd_url'].endswith('/'):
            agentConfig['dd_url'] = agentConfig['dd_url'][:-1]

        # Extra checks.d path
        # the linux directory is set by default
        if config.has_option('Main', 'additional_checksd'):
            agentConfig['additional_checksd'] = config.get('Main', 'additional_checksd')
        elif get_os() == 'windows':
            # default windows location
            common_path = _windows_commondata_path()
            agentConfig['additional_checksd'] = os.path.join(common_path, 'Datadog', 'checks.d')

        # Whether also to send to Pup
        if config.has_option('Main', 'use_pup'):
            agentConfig['use_pup'] = config.get('Main', 'use_pup').lower() in ("yes", "true")
        else:
            agentConfig['use_pup'] = True

        if agentConfig['use_pup']:
            if config.has_option('Main', 'pup_url'):
                agentConfig['pup_url'] = config.get('Main', 'pup_url')
            else:
                agentConfig['pup_url'] = 'http://localhost:17125'

            pup_port = 17125
            if config.has_option('Main', 'pup_port'):
                agentConfig['pup_port'] = int(config.get('Main', 'pup_port'))

        # Increases the frequency of statsd metrics when only sending to Pup
        if not agentConfig['use_dd'] and agentConfig['use_pup']:
            dogstatsd_interval = PUP_STATSD_FREQUENCY

        if not agentConfig['use_dd'] and not agentConfig['use_pup']:
            sys.stderr.write("Please specify at least one endpoint to send metrics to. This can be done in datadog.conf.")
            exit(2)

        # Which API key to use
        agentConfig['api_key'] = config.get('Main', 'api_key')

        # local traffic only? Default to no
        agentConfig['non_local_traffic'] = False
        if config.has_option('Main', 'non_local_traffic'):
            agentConfig['non_local_traffic'] = config.get('Main', 'non_local_traffic').lower() in ("yes", "true")

        # DEPRECATED
        if config.has_option('Main', 'use_ec2_instance_id'):
            use_ec2_instance_id = config.get('Main', 'use_ec2_instance_id')
            # translate yes into True, the rest into False
            agentConfig['use_ec2_instance_id'] = (use_ec2_instance_id.lower() == 'yes')

        if config.has_option('Main', 'check_freq'):
            try:
                agentConfig['check_freq'] = int(config.get('Main', 'check_freq'))
            except:
                pass

        # Disable Watchdog (optionally)
        if config.has_option('Main', 'watchdog'):
            if config.get('Main', 'watchdog').lower() in ('no', 'false'):
                agentConfig['watchdog'] = False

        # Optional graphite listener
        if config.has_option('Main', 'graphite_listen_port'):
            agentConfig['graphite_listen_port'] = \
                int(config.get('Main', 'graphite_listen_port'))
        else:
            agentConfig['graphite_listen_port'] = None

        # Dogstatsd config
        dogstatsd_defaults = {
            'dogstatsd_port': 8125,
            'dogstatsd_target': 'http://localhost:17123',
            'dogstatsd_interval': dogstatsd_interval,
            'dogstatsd_normalize': 'yes',
        }
        for key, value in dogstatsd_defaults.iteritems():
            if config.has_option('Main', key):
                agentConfig[key] = config.get('Main', key)
            else:
                agentConfig[key] = value

        # normalize 'yes'/'no' to boolean
        dogstatsd_defaults['dogstatsd_normalize'] = _is_affirmative(dogstatsd_defaults['dogstatsd_normalize'])

        # optionally send dogstatsd data directly to the agent.
        if config.has_option('Main', 'dogstatsd_use_ddurl'):
            use_ddurl = _is_affirmative(config.get('Main', 'dogstatsd_use_ddurl'))
            if use_ddurl:
                agentConfig['dogstatsd_target'] = agentConfig['dd_url']

        # Optional config
        # FIXME not the prettiest code ever...
        if config.has_option('Main', 'use_mount'):
            agentConfig['use_mount'] = config.get('Main', 'use_mount').lower() in ("yes", "true", "1")

        if config.has_option('Main', 'autorestart'):
            agentConfig['autorestart'] = config.get('Main', 'autorestart').lower() in ("yes", "true", "1")

        if config.has_option('datadog', 'ddforwarder_log'):
            agentConfig['has_datadog'] = True

        # Dogstream config
        if config.has_option("Main", "dogstream_log"):
            # Older version, single log support
            log_path = config.get("Main", "dogstream_log")
            if config.has_option("Main", "dogstream_line_parser"):
                agentConfig["dogstreams"] = ':'.join([log_path, config.get("Main", "dogstream_line_parser")])
            else:
                agentConfig["dogstreams"] = log_path

        elif config.has_option("Main", "dogstreams"):
            agentConfig["dogstreams"] = config.get("Main", "dogstreams")

        if config.has_option("Main", "nagios_perf_cfg"):
            agentConfig["nagios_perf_cfg"] = config.get("Main", "nagios_perf_cfg")

        if config.has_section('WMI'):
            agentConfig['WMI'] = {}
            for key, value in config.items('WMI'):
                agentConfig['WMI'][key] = value

    except ConfigParser.NoSectionError, e:
        sys.stderr.write('Config file not found or incorrectly formatted.\n')
        sys.exit(2)

    except ConfigParser.ParsingError, e:
        sys.stderr.write('Config file not found or incorrectly formatted.\n')
        sys.exit(2)

    except ConfigParser.NoOptionError, e:
        sys.stderr.write('There are some items missing from your config file, but nothing fatal [%s]' % e)

    # Storing proxy settings in the agentConfig
    agentConfig['proxy_settings'] = get_proxy(agentConfig)
    if agentConfig.get('ca_certs', None) is None:
        agentConfig['ssl_certificate'] = get_ssl_certificate(get_os(), 'datadog-cert.pem')
    else:
        agentConfig['ssl_certificate'] = agentConfig['ca_certs']

    return agentConfig


def get_system_stats():
    systemStats = {
        'machine': platform.machine(),
        'platform': sys.platform,
        'processor': platform.processor(),
        'pythonV': platform.python_version(),
    }

    if sys.platform == 'linux2':
        grep = subprocess.Popen(['grep', 'model name', '/proc/cpuinfo'], stdout=subprocess.PIPE, close_fds=True)
        wc = subprocess.Popen(['wc', '-l'], stdin=grep.stdout, stdout=subprocess.PIPE, close_fds=True)
        systemStats['cpuCores'] = int(wc.communicate()[0])

    if sys.platform == 'darwin':
        systemStats['cpuCores'] = int(subprocess.Popen(['sysctl', 'hw.ncpu'], stdout=subprocess.PIPE, close_fds=True).communicate()[0].split(': ')[1])

    if sys.platform.find('freebsd') != -1:
        systemStats['cpuCores'] = int(subprocess.Popen(['sysctl', 'hw.ncpu'], stdout=subprocess.PIPE, close_fds=True).communicate()[0].split(': ')[1])

    if sys.platform == 'linux2':
        systemStats['nixV'] = platform.dist()

    elif sys.platform == 'darwin':
        systemStats['macV'] = platform.mac_ver()

    elif sys.platform.find('freebsd') != -1:
        version = platform.uname()[2]
        systemStats['fbsdV'] = ('freebsd', version, '')  # no codename for FreeBSD

    return systemStats


def set_win32_cert_path():
    """In order to use tornado.httpclient with the packaged .exe on Windows we
    need to override the default ceritifcate location which is based on the path
    to tornado and will give something like "C:\path\to\program.exe\tornado/cert-file".

    If pull request #379 is accepted (https://github.com/facebook/tornado/pull/379) we
    will be able to override this in a clean way. For now, we have to monkey patch
    tornado.httpclient._DEFAULT_CA_CERTS
    """
    if hasattr(sys, 'frozen'):
        # we're frozen - from py2exe
        prog_path = os.path.dirname(sys.executable)
        crt_path = os.path.join(prog_path, 'ca-certificates.crt')
    else:
        cur_path = os.path.dirname(__file__)
        crt_path = os.path.join(cur_path, 'ca-certificates.crt')
    import tornado.simple_httpclient
    tornado.simple_httpclient._DEFAULT_CA_CERTS = crt_path

def get_proxy(agentConfig, use_system_settings=False):
    proxy_settings = {}

    # First we read the proxy configuration from datadog.conf
    proxy_host = agentConfig.get('proxy_host', None)
    if proxy_host is not None and not use_system_settings:
        proxy_settings['host'] = proxy_host
        try:
            proxy_settings['port'] = int(agentConfig.get('proxy_port', 3128))
        except ValueError:
            log.error('Proxy port must be an Integer. Defaulting it to 3128')
            proxy_settings['port'] = 3128

        proxy_settings['user'] = agentConfig.get('proxy_user', None)
        proxy_settings['password'] = agentConfig.get('proxy_password', None)
        proxy_settings['system_settings'] = False
        log.debug("Proxy Settings %s" % str(proxy_settings))
        return proxy_settings

    # If no proxy configuration was specified in datadog.conf
    # We try to read it from the system settings
    try:
        import urllib
        proxies = urllib.getproxies()
        proxy = proxies.get('https', None)
        try:
            proxy = proxy.split('://')[1]
        except Exception:
            pass
        split = proxy.split(':')
        proxy_settings['host'] = split[0]
        proxy_settings['port'] = split[1]
        proxy_settings['user'] = None
        proxy_settings['password'] = None
        proxy_settings['system_settings'] = True
        if '@' in proxy_settings['host']:
            split = proxy_settings['host'].split('@')[0].split(':')
            proxy_settings['user'] = split[0]
            if len(split) == 2:
                proxy_settings['password'] = split[1]

        log.debug("Proxy Settings %s" % str(proxy_settings))
        return proxy_settings
    except Exception, e:
        log.debug("Error while trying to fetch proxy settings using urllib %s. Proxy is probably not set" % str(e))

    return {'host': None,
            'port': None,
            'user': None,
            'password': None,
            'system_settings': False
            }


def get_confd_path(osname):

    bad_path = ''
    if osname == 'windows':
        try:
            return _windows_confd_path()
        except PathNotFound, e:
            if len(e.args) > 0:
                bad_path = e.args[0]
    else:
        try:
            return _unix_confd_path()
        except PathNotFound, e:
            if len(e.args) > 0:
                bad_path = e.args[0]

    cur_path = os.path.dirname(os.path.realpath(__file__))
    cur_path = os.path.join(cur_path, 'conf.d')

    if os.path.exists(cur_path):
        return cur_path

    log.error("No conf.d folder found at '%s' or in the directory where the agent is currently deployed.\n" % bad_path)
    sys.exit(3)


def get_checksd_path(osname):
    try:
        if osname == 'windows':
            return _windows_checksd_path()
        else:
            return _unix_checksd_path()
    except PathNotFound, e:
        if len(e.args) > 0:
            log.error("No checks.d folder found in '%s'.\n" % e.args[0])
        else:
            log.error("No checks.d folder found.\n")
    sys.exit(3)


def get_ssl_certificate(osname, filename):
    # The SSL certificate is needed by tornado in case of connection through a proxy
    if osname == 'windows':
        if hasattr(sys, 'frozen'):
            # we're frozen - from py2exe
            prog_path = os.path.dirname(sys.executable)
            path = os.path.join(prog_path, filename)
        else:
            cur_path = os.path.dirname(__file__)
            path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            log.debug("Certificate file found at %s" % str(path))
            return path

    else:
        cur_path = os.path.dirname(os.path.realpath(__file__))
        path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            return path


    log.info("Certificate file NOT found at %s" % str(path))
    return None


def load_check_directory(agentConfig):
    ''' Return the checks from checks.d. Only checks that have a configuration
    file in conf.d will be returned. '''
    from util import yaml, yLoader
    from checks import AgentCheck

    checks = {}

    osname = get_os()
    checks_paths = (glob.glob(os.path.join(path, '*.py')) for path
                    in [agentConfig['additional_checksd'], get_checksd_path(osname)])
    confd_path = get_confd_path(osname)

    # For backwards-compatability with old style checks, we have to load every
    # checks.d module and check for a corresponding config OR check if the old
    # config will "activate" the check.
    #
    # Once old-style checks aren't supported, we'll just read the configs and
    # import the corresponding check module
    for check in itertools.chain(*checks_paths):
        check_name = os.path.basename(check).split('.')[0]
        if check_name in checks:
            log.debug('Skipping check %s because it has already been loaded from another location', check)
            continue
        try:
            check_module = imp.load_source('checksd_%s' % check_name, check)
        except:
            log.exception('Unable to import check module %s.py from checks.d' % check_name)
            continue

        check_class = None
        classes = inspect.getmembers(check_module, inspect.isclass)
        for name, clsmember in classes:
            if clsmember == AgentCheck:
                continue
            if issubclass(clsmember, AgentCheck):
                check_class = clsmember
                if AgentCheck in clsmember.__bases__:
                    continue
                else:
                    break

        if not check_class:
            log.error('No check class (inheriting from AgentCheck) found in %s.py' % check_name)
            continue

        # Check if the config exists OR we match the old-style config
        conf_path = os.path.join(confd_path, '%s.yaml' % check_name)
        if os.path.exists(conf_path):
            f = open(conf_path)
            try:
                check_config = yaml.load(f.read(), Loader=yLoader)
                assert check_config is not None
                f.close()
            except:
                f.close()
                log.exception("Unable to parse yaml config in %s" % conf_path)
                continue
        elif hasattr(check_class, 'parse_agent_config'):
            # FIXME: Remove this check once all old-style checks are gone
            try:
                check_config = check_class.parse_agent_config(agentConfig)
            except Exception, e:
                continue
            if not check_config:
                continue
            d = [
                "Configuring %s in datadog.conf is deprecated." % (check_name),
                "Please use conf.d. In a future release, support for the",
                "old style of configuration will be dropped.",
            ]
            log.warn(" ".join(d))

        else:
            log.debug('No conf.d/%s.yaml found for checks.d/%s.py' % (check_name, check_name))
            continue

        # Look for the per-check config, which *must* exist
        if not check_config.get('instances'):
            log.error("Config %s is missing 'instances'" % conf_path)
            continue

        # Accept instances as a list, as a single dict, or as non-existant
        instances = check_config.get('instances', {})
        if type(instances) != type([]):
            instances = [instances]

        # Init all of the check's classes with
        init_config = check_config.get('init_config', {})
        # init_config: in the configuration triggers init_config to be defined
        # to None.
        if init_config is None:
            init_config = {}

        instances = check_config['instances']
        try:
            c = check_class(check_name, init_config=init_config,
                            agentConfig=agentConfig, instances=instances)
        except TypeError, e:
            # Backwards compatibility for checks which don't support the
            # instances argument in the constructor.
            c = check_class(check_name, init_config=init_config,
                            agentConfig=agentConfig)
            c.instances = instances

        checks[check_name] = c

        # Add custom pythonpath(s) if available
        if 'pythonpath' in check_config:
            pythonpath = check_config['pythonpath']
            if not isinstance(pythonpath, list):
                pythonpath = [pythonpath]
            sys.path.extend(pythonpath)

        log.debug('Loaded check.d/%s.py' % check_name)

    log.info('checks.d checks: %s' % checks.keys())
    return checks.values()


#
# logging


def get_log_format(logger_name):
    return '%%(asctime)s | %%(levelname)s | dd.%s | %%(name)s(%%(filename)s:%%(lineno)s) | %%(message)s' % logger_name


def get_syslog_format(logger_name):
    return '%%(levelname)s | dd.%s | %%(name)s(%%(filename)s:%%(lineno)s) | %%(message)s' % logger_name


def get_logging_config(cfg_path=None):
    logging_config = {
        'log_level': None,
        'collector_log_file': '/var/log/datadog/collector.log',
        'forwarder_log_file': '/var/log/datadog/forwarder.log',
        'dogstatsd_log_file': '/var/log/datadog/dogstatsd.log',
        'pup_log_file': '/var/log/datadog/pup.log',
        'log_to_syslog': True,
        'syslog_host': None,
        'syslog_port': None,
    }

    config_path = get_config_path(cfg_path, os_name=get_os())
    config = ConfigParser.ConfigParser()
    config.readfp(skip_leading_wsp(open(config_path)))

    if config.has_section('handlers') or config.has_section('loggers') or config.has_section('formatters'):
        sys.stderr.write("Python logging config is no longer supported and will be ignored.\nTo configure logging, update the logging portion of 'datadog.conf' to match:\n  'https://github.com/DataDog/dd-agent/blob/master/datadog.conf.example'.\n")

    for option in logging_config:
        if config.has_option('Main', option):
            logging_config[option] = config.get('Main', option)

    levels = {
        'CRITICAL': logging.CRITICAL,
        'DEBUG': logging.DEBUG,
        'ERROR': logging.ERROR,
        'FATAL': logging.FATAL,
        'INFO': logging.INFO,
        'WARN': logging.WARN,
        'WARNING': logging.WARNING,
    }
    if config.has_option('Main', 'log_level'):
        logging_config['log_level'] = levels.get(config.get('Main', 'log_level'))

    if config.has_option('Main', 'log_to_syslog'):
        logging_config['log_to_syslog'] = config.get('Main', 'log_to_syslog').strip().lower() in ['yes', 'true', 1]

    if config.has_option('Main', 'syslog_host'):
        host = config.get('Main', 'syslog_host').strip()
        if host:
            logging_config['syslog_host'] = host
        else:
            logging_config['syslog_host'] = None

    if config.has_option('Main', 'syslog_port'):
        port = config.get('Main', 'syslog_port').strip()
        try:
            logging_config['syslog_port'] = int(port)
        except:
            logging_config['syslog_port'] = None

    return logging_config


def initialize_logging(logger_name):
    try:
        if get_os() == 'windows':
            logging.config.fileConfig(get_config_path())

        else:
            logging_config = get_logging_config()

            logging.basicConfig(
                format=get_log_format(logger_name),
                level=logging_config['log_level'] or logging.INFO,
            )

            # set up file loggers
            log_file = logging_config.get('%s_log_file' % logger_name)
            if log_file is not None:
                # make sure the log directory is writeable
                # NOTE: the entire directory needs to be writable so that rotation works
                if os.access(os.path.dirname(log_file), os.R_OK | os.W_OK):
                    file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=LOGGING_MAX_BYTES, backupCount=1)
                    file_handler.setFormatter(logging.Formatter(get_log_format(logger_name)))
                    root_log = logging.getLogger()
                    root_log.addHandler(file_handler)
                else:
                    sys.stderr.write("Log file is unwritable: '%s'\n" % log_file)

            # set up syslog
            if logging_config['log_to_syslog']:
                try:
                    from logging.handlers import SysLogHandler

                    if logging_config['syslog_host'] is not None and logging_config['syslog_port'] is not None:
                        sys_log_addr = (logging_config['syslog_host'], logging_config['syslog_port'])
                    else:
                        sys_log_addr = "/dev/log"
                        # Special-case macs
                        if sys.platform == 'darwin':
                            sys_log_addr = "/var/run/syslog"

                    handler = SysLogHandler(address=sys_log_addr, facility=SysLogHandler.LOG_DAEMON)
                    handler.setFormatter(logging.Formatter(get_syslog_format(logger_name)))
                    root_log = logging.getLogger()
                    root_log.addHandler(handler)
                except Exception, e:
                    sys.stderr.write("Error setting up syslog: '%s'\n" % str(e))
                    traceback.print_exc()

    except Exception, e:
        sys.stderr.write("Couldn't initialize logging: %s\n" % str(e))
        traceback.print_exc()

        # if config fails entirely, enable basic stdout logging as a fallback
        logging.basicConfig(
            format=get_log_format(logger_name),
            level=logging.INFO,
        )

    # re-get the log after logging is initialized
    global log
    log = logging.getLogger(__name__)
