import aiohttp
import asyncio
import json
import logging
from unittest.mock import Mock

import pytest

from aiorpcx import JSONRPCv1, JSONRPCv2, JSONRPCLoose, RPCError, ignore_after, Request
from electrumx.lib.coins import Bitcoin, BitcoinCash, CoinError, Bitzeny, Dash, Komodo, Zcash, ZcashTestnet
from electrumx.server.daemon import Daemon, DaemonError, FakeEstimateFeeDaemon, ZcashZebraDaemon
from electrumx.server.session import ElectrumX, ZcashElectrumX


coin = BitcoinCash

# These should be full, canonical URLs
urls = ['http://rpc_user:rpc_pass@127.0.0.1:8332/',
        'http://rpc_user:rpc_pass@192.168.0.1:8332/']


@pytest.fixture(params=[BitcoinCash, Bitzeny])
def daemon(request):
    coin = request.param
    return coin.DAEMON(coin, ','.join(urls))


@pytest.fixture(params=[Dash])
def dash_daemon(request):
    coin = request.param
    return coin.DAEMON(coin, ','.join(urls))


class ResponseBase:

    def __init__(self, headers, status):
        self.headers = headers
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        pass


class JSONResponse(ResponseBase):
    protocol = JSONRPCv1

    def __init__(self, result, msg_id, status=200):
        super().__init__({'Content-Type': 'application/json'}, status)
        self.result = result
        self.msg_id = msg_id

    async def json(self, loads=json.loads):
        if isinstance(self.msg_id, int):
            message = self.protocol.response_message(self.result, self.msg_id)
        else:
            parts = [self.protocol.response_message(item, msg_id)
                     for item, msg_id in zip(self.result, self.msg_id)]
            message = self.protocol.batch_message_from_parts(parts)
        return loads(message.decode())


class HTMLResponse(ResponseBase):

    def __init__(self, text, reason, status):
        super().__init__({'Content-Type': 'text/html; charset=ISO-8859-1'},
                         status)
        self._text = text
        self.reason = reason

    async def text(self):
        return self._text


class ClientSessionGood:
    '''Imitate aiohttp for testing purposes.'''

    def __init__(self, *triples):
        self.triples = triples  # each a (method, args, result)
        self.count = 0
        self.expected_url = urls[0]

    def post(self, url, data=""):
        assert url == self.expected_url
        request, request_id = JSONRPCLoose.message_to_item(data.encode())
        method, args, result = self.triples[self.count]
        self.count += 1
        if isinstance(request, Request):
            assert request.method == method
            assert request.args == args
            return JSONResponse(result, request_id)
        else:
            batch = request
            assert isinstance(batch, list)
            request_ids = []
            for payload, args in zip(batch, args):
                assert payload['method'] == method
                assert payload['params'] == args
                request_ids.append(payload['id'])
            return JSONResponse(result, request_ids)


class ClientSessionJSONWithCharset(ClientSessionGood):
    '''Return a JSON response with a charset media type parameter.'''

    def post(self, url, data=""):
        response = super().post(url, data)
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        return response


class ClientSessionNumericVerbosity(ClientSessionGood):
    '''Reject boolean verbosity in single and batched getblock requests.'''

    def post(self, url, data=""):
        payload = json.loads(data)
        requests = payload if isinstance(payload, list) else [payload]
        for request in requests:
            assert request['method'] == 'getblock'
            assert type(request['params'][1]) is int
        return super().post(url, data)


class ClientSessionV2(ClientSessionGood):
    '''Check outgoing JSON-RPC 2.0 requests and return 2.0 envelopes.'''

    def post(self, url, data=""):
        payload = json.loads(data)
        requests = payload if isinstance(payload, list) else [payload]
        for request in requests:
            assert request['jsonrpc'] == '2.0'
            assert isinstance(request['params'], list)
        response = super().post(url, data)
        response.protocol = JSONRPCv2
        return response


class ClientSessionReorderedBatch(ClientSessionGood):
    '''Return a valid batch response in a different order from its request.'''

    def post(self, url, data=""):
        response = super().post(url, data)
        if isinstance(response.msg_id, list):
            return JSONResponse(response.result[::-1], response.msg_id[::-1])
        return response


class ClientSessionSingleBatchResponse(ClientSessionGood):
    '''Return only the first batch response as an object, not an array.'''

    protocol = JSONRPCv1
    id_offset = 0

    def post(self, url, data=""):
        response = super().post(url, data)
        if isinstance(response.msg_id, list):
            response = JSONResponse(response.result[0], response.msg_id[0] + self.id_offset)
        response.protocol = self.protocol
        return response


class ClientSessionBadAuth:

    def post(self, url, data=""):
         return HTMLResponse('', 'Unauthorized', 401)


class ClientSessionWorkQueueFull(ClientSessionGood):

    def post(self, url, data=""):
        self.post = super().post
        return HTMLResponse('Work queue depth exceeded',
                            'Internal server error', 500)


class ClientSessionPostError(ClientSessionGood):

    def __init__(self, exception, *args):
        super().__init__(*args)
        self.exception = exception
        self.n = 0

    def post(self, url, data=""):
        self.n += 1
        if self.n == 1:
            raise self.exception
        return super().post(url, data)


class ClientSessionFailover(ClientSessionGood):

    def post(self, url, data=""):
        # If not failed over; simulate disconnecting
        if url == self.expected_url:
            raise aiohttp.ServerDisconnectedError
        else:
            self.expected_url = urls[1]
            return super().post(url, data)


def in_caplog(caplog, message, count=1):
    return sum(message in record.message
               for record in caplog.records) == count

#
# Tests
#

@pytest.mark.asyncio
async def test_set_urls_bad():
    with pytest.raises(CoinError):
        Daemon(coin, '')
    with pytest.raises(CoinError):
        Daemon(coin, 'a')


@pytest.mark.asyncio
async def test_set_urls_one(caplog):
    with caplog.at_level(logging.INFO):
        daemon = Daemon(coin, urls[0])
        assert daemon.current_url() == urls[0]
        assert len(daemon.urls) == 1
        logged_url = daemon.logged_url()
        assert logged_url == '127.0.0.1:8332/'
        assert in_caplog(caplog, f'daemon #1 at {logged_url} (current)')


@pytest.mark.asyncio
async def test_set_urls_two(caplog):
    with caplog.at_level(logging.INFO):
        daemon = Daemon(coin, ','.join(urls))
        assert daemon.current_url() == urls[0]
        assert len(daemon.urls) == 2
        logged_url = daemon.logged_url()
        assert logged_url == '127.0.0.1:8332/'
        assert in_caplog(caplog, f'daemon #1 at {logged_url} (current)')
        assert in_caplog(caplog, 'daemon #2 at 192.168.0.1:8332')


@pytest.mark.asyncio
async def test_set_urls_short():
    no_prefix_urls = ['/'.join(part for part in url.split('/')[2:])
                      for url in urls]
    daemon = Daemon(coin, ','.join(no_prefix_urls))
    assert daemon.current_url() == urls[0]
    assert len(daemon.urls) == 2

    no_slash_urls = [url[:-1] for url in urls]
    daemon = Daemon(coin, ','.join(no_slash_urls))
    assert daemon.current_url() == urls[0]
    assert len(daemon.urls) == 2

    no_port_urls = [url[:url.rfind(':')] for url in urls]
    daemon = Daemon(coin, ','.join(no_port_urls))
    assert daemon.current_url() == urls[0]
    assert len(daemon.urls) == 2


@pytest.mark.asyncio
async def test_failover_good(caplog):
    daemon = Daemon(coin, ','.join(urls))
    with caplog.at_level(logging.INFO):
        result = daemon.failover()
    assert result is True
    assert daemon.current_url() == urls[1]
    logged_url = daemon.logged_url()
    assert in_caplog(caplog, f'failing over to {logged_url}')
    # And again
    result = daemon.failover()
    assert result is True
    assert daemon.current_url() == urls[0]


@pytest.mark.asyncio
async def test_failover_fail(caplog):
    daemon = Daemon(coin, urls[0])
    with caplog.at_level(logging.INFO):
        result = daemon.failover()
    assert result is False
    assert daemon.current_url() == urls[0]
    assert not in_caplog(caplog, f'failing over')


@pytest.mark.asyncio
async def test_height(daemon):
    assert daemon.cached_height() is None
    height = 300
    daemon.session = ClientSessionGood(('getblockcount', [], height))
    assert await daemon.height() == height
    assert daemon.cached_height() == height


@pytest.mark.asyncio
async def test_height_accepts_json_with_charset(daemon):
    height = 300
    daemon.session = ClientSessionJSONWithCharset(('getblockcount', [], height))
    assert await daemon.height() == height
    assert daemon.cached_height() == height


@pytest.mark.asyncio
async def test_broadcast_transaction(daemon):
    raw_tx = 'deadbeef'
    tx_hash = 'hash'
    daemon.session = ClientSessionGood(('sendrawtransaction', [raw_tx], tx_hash))
    assert await daemon.broadcast_transaction(raw_tx) == tx_hash

@pytest.mark.asyncio
async def test_broadcast_package(daemon):
    package = ["deadbeef", "deadc0de", "facefeed"]
    result = {"package_msg": "success"}
    daemon.session = ClientSessionGood(('submitpackage', [package], result))
    assert await daemon.broadcast_package(package) == result

@pytest.mark.asyncio
async def test_relayfee(daemon):
    if isinstance(daemon, FakeEstimateFeeDaemon):
        sats = daemon.coin.ESTIMATE_FEE
    else:
        sats = 2
    response = {"relayfee": sats, "other:": "cruft"}
    daemon.session = ClientSessionGood(('getnetworkinfo', [], response))
    assert await daemon.relayfee() == sats


@pytest.mark.asyncio
async def test_mempool_info(daemon):
    bitcoin_per_kvb = 0.00002123
    response = {
        'mempoolminfee': bitcoin_per_kvb,
        'minrelaytxfee': bitcoin_per_kvb,
        'incrementalrelayfee': bitcoin_per_kvb,
        'other': 'cruft',
    }
    daemon.session = ClientSessionGood(('getmempoolinfo', [], response))
    expected_result = dict(response)
    del expected_result['other']
    assert await daemon.mempool_info() == expected_result


@pytest.mark.asyncio
async def test_mempool_txids_hum(daemon):
    hashes = ['hex_hash1', 'hex_hash2']
    daemon.session = ClientSessionGood(('getrawmempool', [], hashes))
    assert await daemon.mempool_txids_hum() == hashes


@pytest.mark.asyncio
async def test_deserialised_block(daemon):
    block_hash = 'block_hash'
    result = {'some': 'mess'}
    daemon.session = ClientSessionNumericVerbosity(('getblock', [block_hash, 1], result))
    assert await daemon.deserialised_block(block_hash) == result


@pytest.mark.asyncio
async def test_estimatefee(daemon):
    method_not_found = RPCError(JSONRPCv1.METHOD_NOT_FOUND, 'nope')
    if isinstance(daemon, FakeEstimateFeeDaemon):
        result = daemon.coin.ESTIMATE_FEE
    else:
        result = -1
    daemon.session = ClientSessionGood(
            ('estimatesmartfee', [], method_not_found),
            ('estimatefee', [2], result)
    )
    assert await daemon.estimatefee(2) == result


@pytest.mark.asyncio
async def test_estimatefee_smart(daemon):
    bad_args = RPCError(JSONRPCv1.INVALID_ARGS, 'bad args')
    if isinstance(daemon, FakeEstimateFeeDaemon):
        return
    rate = 0.0002
    result = {'feerate': rate}
    daemon.session = ClientSessionGood(
        ('estimatesmartfee', [], bad_args),
        ('estimatesmartfee', [2], result)
    )
    assert await daemon.estimatefee(2) == rate

    # Test the rpc_available_cache is used
    daemon.session = ClientSessionGood(('estimatesmartfee', [2], result))
    assert await daemon.estimatefee(2) == rate


@pytest.mark.asyncio
async def test_getrawtransaction(daemon):
    hex_hash = 'deadbeef'
    simple = 'tx_in_hex'
    verbose = {'hex': hex_hash, 'other': 'cruft'}
    # Test False is converted to 0 - old daemon's reject False
    daemon.session = ClientSessionGood(('getrawtransaction', [hex_hash, 0], simple))
    assert await daemon.getrawtransaction(hex_hash) == simple

    # Test True is converted to 1
    daemon.session = ClientSessionGood(('getrawtransaction', [hex_hash, 1], verbose))
    assert await daemon.getrawtransaction(
        hex_hash, True) == verbose


@pytest.mark.asyncio
async def test_protx(dash_daemon):
    protx_hash = 'deadbeaf'
    dash_daemon.session = ClientSessionGood(('protx', ['info', protx_hash], {}))
    assert await dash_daemon.protx(['info', protx_hash]) == {}


# Batch tests

@pytest.mark.asyncio
async def test_empty_send(daemon):
    first = 5
    count = 0
    daemon.session = ClientSessionGood(('getblockhash', [], []))
    assert await daemon.block_hex_hashes(first, count) == []


@pytest.mark.asyncio
async def test_block_hex_hashes(daemon):
    first = 5
    count = 3
    hashes = [f'hex_hash{n}' for n in range(count)]
    daemon.session = ClientSessionGood(('getblockhash',
                                        [[n] for n in range(first, first + count)],
                                        hashes))
    assert await daemon.block_hex_hashes(first, count) == hashes


@pytest.mark.asyncio
async def test_raw_blocks(daemon):
    count = 3
    hex_hashes = [f'hex_hash{n}' for n in range(count)]
    args_list = [[hex_hash, 0] for hex_hash in hex_hashes]
    iterable = (hex_hash for hex_hash in hex_hashes)
    blocks = ["00", "019a", "02fe"]
    blocks_raw = [bytes.fromhex(block) for block in blocks]
    daemon.session = ClientSessionNumericVerbosity(('getblock', args_list, blocks))
    assert await daemon.raw_blocks(iterable) == blocks_raw


@pytest.mark.asyncio
async def test_raw_blocks_accept_reordered_batch_response(daemon):
    hashes = ['block0', 'block1']
    daemon.session = ClientSessionReorderedBatch(
        ('getblock', [[block_hash, 0] for block_hash in hashes], ['00', 'ff']))
    assert await daemon.raw_blocks(hashes) == [b'\x00', b'\xff']


@pytest.mark.asyncio
async def test_reordered_batch_keeps_error_at_request_position(daemon):
    daemon.session = ClientSessionReorderedBatch(
        ('getrawtransaction', [['tx0', 0], ['missing', 0]],
         ['00', RPCError(-5, 'Not found')]))
    assert await daemon.getrawtransactions(['tx0', 'missing']) == [b'\x00', None]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'duplicate', 'unexpected', 'extra', 'empty', 'no_id'])
async def test_reject_mismatched_batch_ids(daemon, monkeypatch, case):
    async def send_data(data):
        first, second = [request['id'] for request in json.loads(data)]
        unexpected = max(first, second) + 1
        ids = {
            'missing': [first],
            'duplicate': [first, first],
            'unexpected': [first, unexpected],
            'extra': [first, second, unexpected],
            'empty': [],
            'no_id': [None, second],
        }[case]
        responses = []
        for response_id in ids:
            response = {'result': '00', 'error': None}
            if response_id is not None:
                response['id'] = response_id
            responses.append(response)
        return responses

    monkeypatch.setattr(daemon, '_send_data', send_data)
    with pytest.raises(DaemonError, match='batch response IDs do not match request IDs'):
        await daemon.raw_blocks(['block0', 'block1'])


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 2])
async def test_batch_processor_rejects_non_array_response(daemon, monkeypatch, count):
    async def send_data(data):
        request = json.loads(data)[0]
        return {'id': request['id'], 'result': '00', 'error': None}

    monkeypatch.setattr(daemon, '_send_data', send_data)
    with pytest.raises(DaemonError, match='non-batch response to batch request'):
        await daemon.raw_blocks([f'block{index}' for index in range(count)])


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [JSONRPCv1, JSONRPCv2])
async def test_single_object_batch_response(daemon, protocol):
    daemon.session = ClientSessionSingleBatchResponse(
        ('getblock', [['block0', 0]], ['00']))
    daemon.session.protocol = protocol
    assert await daemon.raw_blocks(['block0']) == [b'\x00']


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [JSONRPCv1, JSONRPCv2])
async def test_single_object_batch_response_checks_id(daemon, protocol):
    daemon.session = ClientSessionSingleBatchResponse(
        ('getblock', [['block0', 0]], ['00']))
    daemon.session.protocol = protocol
    daemon.session.id_offset = 1
    with pytest.raises(DaemonError, match='batch response IDs do not match request IDs'):
        await daemon.raw_blocks(['block0'])


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [JSONRPCv1, JSONRPCv2])
async def test_single_object_batch_response_preserves_error(daemon, protocol):
    error = RPCError(-5, 'Not found')
    daemon.session = ClientSessionSingleBatchResponse(
        ('getblock', [['missing', 0]], [error]),
        ('getrawtransaction', [['missing', 0]], [error]))
    daemon.session.protocol = protocol
    with pytest.raises(DaemonError, match='Not found'):
        await daemon.raw_blocks(['missing'])
    assert await daemon.getrawtransactions(['missing']) == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', [JSONRPCv1, JSONRPCv2])
async def test_reject_single_object_for_multi_request_batch(daemon, protocol):
    daemon.session = ClientSessionSingleBatchResponse(
        ('getblock', [['block0', 0], ['block1', 0]], ['00', 'ff']))
    daemon.session.protocol = protocol
    with pytest.raises(DaemonError, match='non-batch response to batch request'):
        await daemon.raw_blocks(['block0', 'block1'])


@pytest.mark.asyncio
async def test_jsonrpc_v2_requests_and_responses(daemon):
    error = RPCError(-5, 'Not found')
    daemon.session = ClientSessionV2(
        ('getblockcount', [], 123),
        ('getblock', ['block0', 1], {'hash': 'block0'}),
        ('getblock', [['block0', 0]], ['00']),
        ('getrawtransaction', ['missing', 0], error),
        ('getrawtransaction', [['tx0', 0], ['missing', 0]], ['00', error]),
        ('getblock', [['missing', 0]], [error]))
    assert await daemon.height() == 123
    assert await daemon.deserialised_block('block0') == {'hash': 'block0'}
    assert await daemon.raw_blocks(['block0']) == [b'\x00']
    with pytest.raises(DaemonError, match='Not found'):
        await daemon.getrawtransaction('missing')
    assert await daemon.getrawtransactions(['tx0', 'missing']) == [b'\x00', None]
    with pytest.raises(DaemonError, match='Not found'):
        await daemon.raw_blocks(['missing'])


@pytest.mark.asyncio
async def test_zebra_daemon_request_compatibility():
    daemon = ZcashZebraDaemon(Zcash, urls[0])
    assert daemon._single_payload('getrawmempool', None)['params'] == ()

    daemon.session = ClientSessionJSONWithCharset(('getblockcount', [], 1))
    assert await daemon.height() == 1

    hashes = ['block0', 'block1']
    daemon.session = ClientSessionNumericVerbosity(
        ('getblock', [[block_hash, 0] for block_hash in hashes], ['00', 'ff']))
    assert await daemon.raw_blocks(hashes) == [b'\x00', b'\xff']

    daemon.session = ClientSessionSingleBatchResponse(('getblock', [['block0', 0]], ['00']))
    assert await daemon.raw_blocks(['block0']) == [b'\x00']

    daemon.session = ClientSessionNumericVerbosity(('getblock', ['block0', 1], {'hash': 'block0'}))
    assert await daemon.deserialised_block('block0') == {'hash': 'block0'}

    # No RPC call should be made for an unavailable fee estimate.
    daemon.session = Mock()
    assert await daemon.estimatefee(1) == -1
    daemon.session.post.assert_not_called()


@pytest.mark.parametrize('coin', [Zcash, ZcashTestnet])
def test_zcash_daemon_and_session_selection(coin):
    assert coin.DAEMON is ZcashZebraDaemon
    assert coin.SESSIONCLS is ZcashElectrumX
    assert coin.SESSIONCLS.PROTOCOL_MAX == (1, 4, 3)


@pytest.mark.parametrize('coin', [Bitcoin, Komodo])
def test_zcash_rpc_limits_do_not_apply_to_other_coins(coin):
    assert coin.DAEMON is Daemon
    assert coin.SESSIONCLS is ElectrumX
    assert coin.SESSIONCLS.PROTOCOL_MAX == (1, 7, 0)


@pytest.fixture
def zcash_session():
    session = object.__new__(ZcashElectrumX)
    session.bump_cost = Mock()
    session.set_request_handlers((1, 4, 3))
    return session


def test_zcash_session_request_handlers(zcash_session):
    handlers = zcash_session.request_handlers
    assert 'mempool.get_info' not in handlers
    assert 'blockchain.transaction.broadcast_package' not in handlers
    assert 'blockchain.transaction.testmempoolaccept' not in handlers
    assert 'blockchain.scriptpubkey.get_history' not in handlers
    assert 'blockchain.scripthash.get_history' in handlers
    assert 'blockchain.relayfee' in handlers
    assert handlers['blockchain.estimatefee'] == zcash_session.phandle_estimatefee


@pytest.mark.asyncio
async def test_zcash_session_fee_estimate_unavailable(zcash_session):
    assert await zcash_session.request_handlers['blockchain.estimatefee'](1) == -1
    zcash_session.bump_cost.assert_called_once_with(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize('number,mode', [(-1, None), (None, None), ('invalid', None),
                                        (1, 'economical'), (1, 'conservative')])
async def test_zcash_session_rejects_invalid_estimatefee_arguments(zcash_session, number, mode):
    with pytest.raises(RPCError):
        await zcash_session.phandle_estimatefee(number, mode)
    zcash_session.bump_cost.assert_not_called()


@pytest.mark.asyncio
async def test_get_raw_transactions(daemon):
    hex_hashes = ['deadbeef0', 'deadbeef1']
    args_list = [[hex_hash, 0] for hex_hash in hex_hashes]
    raw_txs_hex = ['fffefdfc', '0a0b0c0d']
    raw_txs = [bytes.fromhex(raw_tx) for raw_tx in raw_txs_hex]
    # Test 0 - old daemon's reject False
    daemon.session = ClientSessionGood(('getrawtransaction', args_list, raw_txs_hex))
    assert await daemon.getrawtransactions(hex_hashes) == raw_txs

    # Test one error
    tx_not_found = RPCError(-1, 'some error message')
    results = ['ff0b7d', tx_not_found]
    raw_txs = [bytes.fromhex(results[0]), None]
    daemon.session = ClientSessionGood(('getrawtransaction', args_list, results))
    assert await daemon.getrawtransactions(hex_hashes) == raw_txs


# Other tests

@pytest.mark.asyncio
async def test_bad_auth(daemon, caplog):
    async with ignore_after(0.1):
        daemon.session = ClientSessionBadAuth()
        await daemon.height()

    assert in_caplog(caplog, "daemon service refused")
    assert in_caplog(caplog, "Unauthorized")


@pytest.mark.asyncio
async def test_workqueue_depth(daemon, caplog):
    daemon.init_retry = 0.01
    height = 125
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionWorkQueueFull(('getblockcount', [], height))
        await daemon.height() == height

    assert in_caplog(caplog, "Work queue depth exceeded")
    assert in_caplog(caplog, "running normally")


@pytest.mark.asyncio
async def test_connection_error(daemon, caplog):
    height = 100
    daemon.init_retry = 0.01
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionPostError(aiohttp.ClientConnectionError,
                                                ('getblockcount', [], height))
        await daemon.height() == height

    assert in_caplog(caplog, "connection problem - check your daemon is running")
    assert in_caplog(caplog, "connection restored")


@pytest.mark.asyncio
async def test_timeout_error(daemon, caplog):
    height = 100
    daemon.init_retry = 0.01
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionPostError(asyncio.TimeoutError,
                                                ('getblockcount', [], height))
        await daemon.height() == height

    assert in_caplog(caplog, "timeout error")


@pytest.mark.asyncio
async def test_disconnected(daemon, caplog):
    height = 100
    daemon.init_retry = 0.01
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionPostError(aiohttp.ServerDisconnectedError,
                                                ('getblockcount', [], height))
        await daemon.height() == height

    assert in_caplog(caplog, "disconnected")
    assert in_caplog(caplog, "connection restored")


@pytest.mark.asyncio
async def test_warming_up(daemon, caplog):
    warming_up = RPCError(-28, 'reading block index')
    height = 100
    daemon.init_retry = 0.01
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionGood(
            ('getblockcount', [], warming_up),
            ('getblockcount', [], height)
        )
        assert await daemon.height() == height

    assert in_caplog(caplog, "starting up checking blocks")
    assert in_caplog(caplog, "running normally")


@pytest.mark.asyncio
async def test_warming_up_batch(daemon, caplog):
    warming_up = RPCError(-28, 'reading block index')
    first = 5
    count = 1
    daemon.init_retry = 0.01
    hashes = ['hex_hash5']
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionGood(('getblockhash', [[first]], [warming_up]),
                                           ('getblockhash', [[first]], hashes))
        assert await daemon.block_hex_hashes(first, count) == hashes

    assert in_caplog(caplog, "starting up checking blocks")
    assert in_caplog(caplog, "running normally")


@pytest.mark.asyncio
async def test_failover(daemon, caplog):
    height = 100
    daemon.init_retry = 0.01
    daemon.max_retry = 0.04
    with caplog.at_level(logging.INFO):
        daemon.session = ClientSessionFailover(('getblockcount', [], height))
        await daemon.height() == height

    assert in_caplog(caplog, "disconnected", 1)
    assert in_caplog(caplog, "failing over")
    assert in_caplog(caplog, "connection restored")
