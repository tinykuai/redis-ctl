from socket import error as SocketError
import hiredis
import logging
import redistrib.command

import base
import models.db
import models.cluster
import models.proxy
import models.node as nm


@base.post_async('/cluster/add')
def add_cluster(request):
    with models.db.update() as c:
        return str(models.cluster.create_cluster(c, request.form['descr']))


@base.post_async('/cluster/launch')
def start_cluster(request):
    cluster_id = int(request.form['cluster_id'])
    try:
        nm.pick_and_launch(
            request.form['host'], int(request.form['port']), cluster_id,
            redistrib.command.start_cluster)
    except SocketError, e:
        logging.exception(e)

        with models.db.update() as c:
            models.cluster.remove_empty_cluster(c, cluster_id)

        raise ValueError('Node disconnected')


@base.post_async('/cluster/set_info')
def set_cluster_info(request):
    cluster_id = int(request.form['cluster_id'])
    descr = request.form.get('descr', '')
    with models.db.update() as c:
        models.cluster.set_info(c, cluster_id, descr)
        if not request.form.get('proxy_host'):
            return
        host = request.form['proxy_host']
        port = int(request.form['proxy_port'])
        models.proxy.attach_to_cluster(c, cluster_id, host, port)


@base.post_async('/cluster/recover_migrate')
def recover_migrate_status(request):
    cluster_id = int(request.form['cluster_id'])
    logging.info('Start - recover cluster migrate #%d', cluster_id)
    with models.db.query() as c:
        for i in nm.contained_in_cluster(c, cluster_id):
            try:
                nm.lock_instance(i[nm.COL_ID], cluster_id)
                redistrib.command.fix_migrating(i[nm.COL_HOST], i[nm.COL_PORT])
            finally:
                nm.unlock_instance(i[nm.COL_ID])
    logging.info('Done - recover cluster migrate #%d', cluster_id)


@base.post_async('/cluster/migrate_slots')
def migrate_slots(request):
    src_host = request.form['src_host']
    src_port = int(request.form['src_port'])
    dst_host = request.form['dst_host']
    dst_port = int(request.form['dst_port'])
    slots = [int(s) for s in request.form['slots'].split(',')]
    with models.db.query() as c:
        i = nm.pick_by(c, src_host, src_port)
    if i[nm.COL_CLUSTER_ID] is None:
        raise ValueError('%s:%d not in cluster' % (src_host, src_port))
    try:
        nm.lock_instance(i[nm.COL_ID], i[nm.COL_CLUSTER_ID])
        redistrib.command.migrate_slots(src_host, src_port, dst_host,
                                        dst_port, slots)
    finally:
        nm.unlock_instance(i[nm.COL_ID])


@base.post_async('/cluster/join')
def join_cluster(request):
    nm.pick_and_expand(request.form['host'], int(request.form['port']),
                       int(request.form['cluster_id']),
                       redistrib.command.join_cluster)

NOT_IN_CLUSTER_MESSAGE = 'not in a cluster'


@base.post_async('/cluster/quit')
def quit_cluster(request):
    host = request.form['host']
    port = int(request.form['port'])
    cluster_id = int(request.form['cluster_id'])
    try:
        nm.quit(host, port, cluster_id, redistrib.command.quit_cluster)
    except SocketError, e:
        logging.exception(e)
        logging.info('Remove instance from cluster on exception')
        nm.free_instance(host, port, cluster_id)
    except hiredis.ProtocolError, e:
        if NOT_IN_CLUSTER_MESSAGE not in e.message:
            raise
        nm.free_instance(host, port, cluster_id)

    with models.db.update() as c:
        models.cluster.remove_empty_cluster(c, cluster_id)


@base.post_async('/cluster/replicate')
def replicate(request):
    nm.pick_and_replicate(
        request.form['master_host'], int(request.form['master_port']),
        request.form['slave_host'], int(request.form['slave_port']),
        redistrib.command.replicate)


@base.get_async('/cluster/autodiscover')
def cluster_auto_discover(request):
    host = request.args['host']
    port = int(request.args['port'])
    try:
        nodes = redistrib.command.list_nodes(host, port, host)[0]
    except StandardError, e:
        logging.exception(e)
        raise ValueError(e)

    unknown_nodes = []
    with models.db.query() as client:
        for n in nodes:
            p = nm.pick_by(client, n.host, n.port)
            if p is None:
                unknown_nodes.append(n)

    return base.json_result([{
        'host': n.host,
        'port': n.port,
        'role': n.role_in_cluster,
    } for n in unknown_nodes])


@base.post_async('/cluster/autojoin')
def cluster_auto_join(request):
    host = request.form['host']
    port = int(request.form['port'])
    try:
        nodes = redistrib.command.list_nodes(host, port, host)[0]
    except StandardError, e:
        logging.exception(e)
        raise ValueError(e)

    cluster_ids = set()
    free_nodes_ids = []
    with models.db.query() as client:
        for n in nodes:
            p = nm.pick_by(client, n.host, n.port)
            if p is None:
                raise ValueError('%s:%d not in db' % (n.host, n.port))

            if p[nm.COL_CLUSTER_ID] is None:
                free_nodes_ids.append(p[nm.COL_ID])
            else:
                cluster_ids.add(p[nm.COL_CLUSTER_ID])

    if len(cluster_ids) > 1:
        raise ValueError('nodes are in different clusters according to db')

    with models.db.update() as client:
        cluster_id = (models.cluster.create_cluster(client, '')
                      if len(cluster_ids) == 0 else cluster_ids.pop())
        try:
            for node_id in free_nodes_ids:
                nm.distribute_free_to(client, node_id, cluster_id)
            return str(cluster_id)
        finally:
            models.cluster.remove_empty_cluster(client, cluster_id)
