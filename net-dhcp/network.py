import itertools
import ipaddress
import logging
import atexit
import socket

import pyroute2
from pyroute2.netlink.rtnl import rtypes
import docker
from flask import request, jsonify

from . import NetDhcpError, app

OPTS_KEY = 'com.docker.network.generic'
OPT_PREFIX = 'devplayer0.net-dhcp'
OPT_BRIDGE = f'{OPT_PREFIX}.bridge'

logger = logging.getLogger('gunicorn.error')

ndb = pyroute2.NDB()
@atexit.register
def close_ndb():
    ndb.close()

client = docker.from_env()
@atexit.register
def close_docker():
    client.close()

def veth_pair(e):
    return f'dh-{e[:12]}', f'{e[:12]}-dh'

def iface_addrs(iface):
    return list(map(lambda a: ipaddress.ip_interface((a['address'], a['prefixlen'])), iface.ipaddr))
def iface_nets(iface):
    return list(map(lambda n: n.network, iface_addrs(iface)))

def get_bridges():
    reserved_nets = set(map(ipaddress.ip_network, map(lambda c: c['Subnet'], \
        itertools.chain.from_iterable(map(lambda i: i['Config'], filter(lambda i: i['Driver'] != 'net-dhcp', \
            map(lambda n: n.attrs['IPAM'], client.networks.list())))))))

    return dict(map(lambda i: (i['ifname'], i), filter(lambda i: i['kind'] == 'bridge' and not \
        set(iface_nets(i)).intersection(reserved_nets), map(lambda i: ndb.interfaces[i.ifname], ndb.interfaces))))

def net_bridge(n):
    return ndb.interfaces[client.networks.get(n).attrs['Options'][OPT_BRIDGE]]

@app.route('/NetworkDriver.GetCapabilities', methods=['POST'])
def net_get_capabilities():
    return jsonify({
        'Scope': 'local',
        'ConnectivityScope': 'global'
    })

@app.route('/NetworkDriver.CreateNetwork', methods=['POST'])
def create_net():
    req = request.get_json(force=True)
    if OPT_BRIDGE not in req['Options'][OPTS_KEY]:
        return jsonify({'Err': 'No bridge provided'}), 400

    desired = req['Options'][OPTS_KEY][OPT_BRIDGE]
    bridges = get_bridges()
    if desired not in bridges:
        return jsonify({'Err': f'Bridge "{desired}" not found (or the specified bridge is already used by Docker)'}), 400

    logger.info('Creating network "%s" (using bridge "%s")', req['NetworkID'], desired)
    return jsonify({})

@app.route('/NetworkDriver.DeleteNetwork', methods=['POST'])
def delete_net():
    return jsonify({})

@app.route('/NetworkDriver.CreateEndpoint', methods=['POST'])
def create_endpoint():
    req = request.get_json(force=True)
    req_iface = req['Interface']

    bridge = net_bridge(req['NetworkID'])
    bridge_addrs = iface_addrs(bridge)

    if_host, if_container = veth_pair(req['EndpointID'])
    logger.info('creating veth pair %s <=> %s', if_host, if_container)
    if_host = (ndb.interfaces.create(ifname=if_host, kind='veth', peer=if_container)
                .set('state', 'up')
                .commit())

    if_container = (ndb.interfaces[if_container]
                    .set('state', 'up')
                    .commit())
    res_iface = {
        'MacAddress': '',
        'Address': '',
        'AddressIPv6': ''
    }

    try:
        if 'MacAddress' not in req_iface or not req_iface['MacAddress']:
            res_iface['MacAddress'] = if_container['address']

        def try_addr(type_):
            addr = None
            k = 'AddressIPv6' if type_ == 'v6' else 'Address'
            if k in req_iface and req_iface[k]:
                # Just validate the address, Docker will add it to the interface for us
                addr = ipaddress.ip_interface(req_iface[k])
                for bridge_addr in bridge_addrs:
                    if addr.ip == bridge_addr.ip:
                        raise NetDhcpError(400, f'Address {addr} is already in use on bridge {bridge["ifname"]}')

                logger.info('Adding address %s to %s', addr, if_container['ifname'])
            elif type_ == 'v4':
                raise NetDhcpError(400, f'DHCP{type_} is currently unsupported')
        try_addr('v4')
        try_addr('v6')

        (bridge
            .add_port(if_host)
            .commit())

        res = jsonify({
            'Interface': res_iface
        })
    except NetDhcpError as e:
        (if_host
            .remove()
            .commit())
        logger.error(e)
        res = jsonify({'Err': str(e)}), e.status
    except Exception as e:
        (if_host
            .remove()
            .commit())
        res = jsonify({'Err': str(e)}), 500
    finally:
        return res

@app.route('/NetworkDriver.EndpointOperInfo', methods=['POST'])
def endpoint_info():
    req = request.get_json(force=True)

    bridge = net_bridge(req['NetworkID'])
    if_host, _if_container = veth_pair(req['EndpointID'])
    if_host = ndb.interfaces[if_host]

    return jsonify({
        'bridge': bridge['ifname'],
        'if_host': {
            'name': if_host['ifname'],
            'mac': if_host['address']
        }
    })

@app.route('/NetworkDriver.DeleteEndpoint', methods=['POST'])
def delete_endpoint():
    req = request.get_json(force=True)

    bridge = net_bridge(req['NetworkID'])
    if_host, _if_container = veth_pair(req['EndpointID'])
    if_host = ndb.interfaces[if_host]

    bridge.del_port(if_host['ifname'])
    (if_host
        .remove()
        .commit())

    return jsonify({})

@app.route('/NetworkDriver.Join', methods=['POST'])
def join():
    req = request.get_json(force=True)

    bridge = net_bridge(req['NetworkID'])
    _if_host, if_container = veth_pair(req['EndpointID'])

    res = {
        'InterfaceName': {
            'SrcName': if_container,
            'DstPrefix': bridge['ifname']
        },
        'StaticRoutes': []
    }
    for route in bridge.routes:
        # TODO: IPv6 routes
        if route['type'] != rtypes['RTN_UNICAST'] or route['family'] != socket.AF_INET:
            continue

        if route['dst'] == '' and 'Gateway' not in res:
            res['Gateway'] = route['gateway']
            continue
        elif route['gateway']:
            res['StaticRoutes'].append({
                'Destination': f'{route["dst"]}/{route["dst_len"]}',
                'RouteType': 0,
                'NextHop': route['gateway']
            })

    logger.info(res)
    return jsonify(res)

@app.route('/NetworkDriver.Leave', methods=['POST'])
def leave():
    return jsonify({})
