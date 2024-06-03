# -*- coding: utf-8 -*-

from aiohttp import web
import electrumx.lib.util as util
from electrumx.lib.hash import hash_to_hex_str
import json


async def format_params(self, request: web.Request):
    params: list
    if request.method == "GET":
        params = json.loads(request.query.get("params", "[]"))
    elif request.content_length:
        json_data = await request.json()
        params = json_data.get("params", [])
    else:
        params = []
    return dict(zip(range(len(params)), params))

class HttpHandler(object):
    PROTOCOL_MIN = (1, 4)
    PROTOCOL_MAX = (1, 4, 3)

    def __init__(self, db):
        # self.transport = transport
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.db = db

    async def all_utxos(self, request):
        params = await format_params(request)
        startkey = params.get(0, None)
        limit = params.get(1, None)
        print('startkey=',startkey)
        print('limit=',limit)
        last_db_key, utxos = await self.db.pageable_utxos(startkey, limit)
        data_list = []
        for utxo in utxos:
            data = {'height': utxo.height,
                    'txid': hash_to_hex_str(utxo.tx_hash),
                    'vout': utxo.tx_pos,
                    'value': utxo.value}
            data_list.append(data)

        res = {'last_key': last_db_key, 'utxos': data_list}
        return web.json_response(res)
