# -*- coding: utf-8 -*-

from aiohttp import web
import electrumx.lib.util as util
from electrumx.lib.hash import hash_to_hex_str


class HttpHandler(object):
    PROTOCOL_MIN = (1, 4)
    PROTOCOL_MAX = (1, 4, 3)

    def __init__(self, db,daemon):
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.db = db
        self.daemon = daemon

    async def all_utxos(self, request):
        startkey = request.query.get("startkey", None)
        limit = request.query.get("limit", 10)
        limit = int(limit)
        print('startkey=', startkey)
        print('limit=', limit)
        last_db_key, utxos = await self.db.pageable_utxos(startkey, limit)
        data_list = []
        txids = {hash_to_hex_str(utxo.tx_hash) for utxo in utxos}

        txs = self.daemon.getrawtransactions(txids)
        print(txs)

        for utxo in utxos:
            print(utxo)
            data = {'height': utxo.height,
                    'txid': hash_to_hex_str(utxo.tx_hash),
                    'vout': utxo.tx_pos,
                    'value': utxo.value}
            data_list.append(data)

        res = {'last_key': last_db_key, 'utxos': data_list}
        return web.json_response(res)

    async def count_utxos(self, request):
        count = await self.db.count_utxos()
        res = {'utxo_count': count}
        return web.json_response(res)
