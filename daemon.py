import sys
import time
import logging
from redistrib.clusternode import Talker, pack_command, ClusterNode
from socket import error as SocketError
from influxdb import InfluxDBClient
from collections import OrderedDict

import config
import file_ipc

INTERVAL = 10
CMD_INFO = pack_command('info')
CMD_CLUSTER_NODES = pack_command('cluster', 'nodes')

INFLUXDB = None
COLUMNS = OrderedDict([
    ('used_memory_rss', 'used_memory_rss'),
    ('connected_clients', 'connected_clients'),
    ('expired_keys', 'expired_keys'),
    ('evicted_keys', 'evicted_keys'),
    ('keyspace_hits', 'keyspace_hits'),
    ('keyspace_misses', 'keyspace_misses'),
    ('used_cpu_sys', 'used_cpu_sys'),
    ('used_cpu_user', 'used_cpu_user'),
])

PRECPU = {}


def _info_detail(t):
    details = dict()
    for line in t.talk_raw(CMD_INFO).split('\n'):
        if len(line) == 0 or line.startswith('#'):
            continue
        r = line.split(':')
        if len(r) != 2:
            continue
        k, v = r
        details[k.strip()] = v.strip()
    return details


def _info_slots(t):
    for line in t.talk_raw(CMD_CLUSTER_NODES).split('\n'):
        if len(line) == 0 or 'fail' in line or 'myself' not in line:
            continue
        node = ClusterNode(*line.split(' '))
        return {
            'node_id': node.node_id,
            'slave': node.role_in_cluster != 'master',
            'master_id': node.master_id if node.master_id != '-' else None,
            'slots': node.assigned_slots,
        }


def _send_to_influxdb(node):
    global PRECPU
    name = '%s:%s' % (node['host'], node['port'])
    points = [
        node['mem'][COLUMNS['used_memory_rss']],
        node['conn'][COLUMNS['connected_clients']],
        node['storage'][COLUMNS['expired_keys']],
        node['storage'][COLUMNS['evicted_keys']],
        node['storage'][COLUMNS['keyspace_hits']],
        node['storage'][COLUMNS['keyspace_misses']],
    ]
    cpu = PRECPU.get(name)
    used_cpu_sys = (float(
        node['cpu'][COLUMNS['used_cpu_sys']] - cpu['used_cpu_sys'])
        * 1000.0 / INTERVAL if cpu else 0)
    used_cpu_user = (float(
        node['cpu'][COLUMNS['used_cpu_user']] - cpu['used_cpu_user'])
        * 1000.0 / INTERVAL if cpu else 0)
    PRECPU[name] = {
        'used_cpu_sys': node['cpu'][COLUMNS['used_cpu_sys']],
        'used_cpu_user': node['cpu'][COLUMNS['used_cpu_user']],
    }
    points.append(used_cpu_sys)
    points.append(used_cpu_user)
    json_body = [{
        "points": [points, ],
        'name': name,
        'columns': COLUMNS.keys(),
    }]
    INFLUXDB.write_points(json_body)


def _info_node(host, port):
    t = Talker(host, port)
    try:
        node_info = _info_slots(t)
        details = _info_detail(t)
        node_info['mem'] = {
            'used_memory_rss': int(details['used_memory_rss']),
            'used_memory_human': details['used_memory_human'],
        }
        node_info['cpu'] = {
            'used_cpu_sys': float(details['used_cpu_sys']),
            'used_cpu_user': float(details['used_cpu_user']),
        }
        node_info['conn'] = {
            'connected_clients': int(details['connected_clients']),
        }
        node_info['storage'] = {
            'expired_keys': int(details['expired_keys']),
            'evicted_keys': int(details['evicted_keys']),
            'keyspace_hits': int(details['keyspace_hits']),
            'keyspace_misses': int(details['keyspace_misses']),
            'aof_enabled': details['aof_enabled'] == '1',
        }
        node_info['stat'] = True
        return node_info
    except StandardError, e:
        logging.error('Fail to retrieve info of %s:%d', host, port)
        logging.exception(e)
        return {'stat': False}


def run():
    while True:
        nodes = file_ipc.read_poll()
        for node in nodes:
            try:
                node.update(_info_node(**node))
                _send_to_influxdb(node)
            except SocketError, e:
                logging.error('Fail to connect to %s:%d',
                              node['host'], node['port'])
                logging.exception(e)
                node['stat'] = False
        logging.info('Total %d nodes', len(nodes))
        try:
            file_ipc.write(nodes)
        except StandardError, e:
            logging.exception(e)

        time.sleep(INTERVAL)


def main():
    conf = config.load('config.yaml' if len(sys.argv) == 1 else sys.argv[1])
    config.init_logging(conf)
    global INFLUXDB, INTERVAL, _send_to_influxdb
    INTERVAL = int(conf.get('interval', INTERVAL))
    if 'influxdb' in conf:
        INFLUXDB = InfluxDBClient(
            conf['influxdb']['host'],
            conf['influxdb']['port'],
            conf['influxdb']['username'],
            conf['influxdb']['password'],
            conf['influxdb']['database'],
        )
    else:
        _send_to_influxdb = lambda _: None
    run()

if __name__ == '__main__':
    main()
