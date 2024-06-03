# -*- coding: utf-8 -*-

from aiohttp import web
import electrumx.lib.util as util



class HttpHandler(object):

    PROTOCOL_MIN = (1, 4)
    PROTOCOL_MAX = (1, 4, 3)

    def __init__(self, db):
        # self.transport = transport
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.db = db


    async def all_utxos(self, request):
        startkey = request.match_info.get('startkey', '')
        limit = request.match_info.get('limit', '')
        last_db_key, utxos = await self.db.pageable_utxos(startkey, limit)
        res= {'last_db_key': last_db_key, 'utxos': utxos}
        return web.json_response(res)


